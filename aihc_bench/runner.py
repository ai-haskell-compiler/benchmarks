from __future__ import annotations

import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import expand_command
from .database import Database
from .git_history import create_worktree, path_exists, remove_worktree
from .measurement import measure_adaptively
from .process import run_command
from .schema import environment_record, new_run_id, result_envelope


@dataclass(frozen=True)
class Cell:
    benchmark: Dict[str, Any]
    configuration: Dict[str, Any]
    commit_sha: str
    artifact: Path
    build_dir: Path
    compile_cwd: Path
    compile_environment: Dict[str, str]
    compile_command: Optional[List[str]]
    run_command: Optional[List[str]]
    unavailable_reason: Optional[str]
    setup_error: Optional[str]


def run_commit(
    *,
    database: Database,
    config: Dict[str, Any],
    experiment_id: str,
    platform_id: str,
    commit: Dict[str, Any],
    aihc_repository: Path,
    root: Path,
    jobs: int,
) -> Dict[str, Any]:
    environment = environment_record(platform_id)
    run_id = new_run_id()
    database.start_attempt(experiment_id, platform_id, commit["sha"], run_id, environment)
    worktree = root / ".cache" / "aihc-worktree"
    measurement_config = config["measurement"]

    if not path_exists(aihc_repository, commit["sha"], config["aihc_compiler_marker"]):
        envelope = result_envelope(
            experiment_id=experiment_id,
            platform_id=platform_id,
            environment=environment,
            commit=commit,
            compiler_status="unavailable",
            unavailable_reason="no_compiler",
            results=[],
            run_id=run_id,
        )
        database.finish_attempt(
            experiment_id,
            platform_id,
            commit["sha"],
            "unavailable",
            envelope,
            unavailable_reason="no_compiler",
        )
        return envelope

    try:
        create_worktree(aihc_repository, worktree, commit["sha"])
        probe = run_command(
            ["nix", "run", f"{worktree}#aihc", "--", "--help"],
            root,
            float(measurement_config["compile_timeout_seconds"]),
        )
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout)[-8192:]
            envelope = result_envelope(
                experiment_id=experiment_id,
                platform_id=platform_id,
                environment=environment,
                commit=commit,
                compiler_status="unavailable",
                unavailable_reason="build_failed",
                results=[],
                run_id=run_id,
            )
            database.finish_attempt(
                experiment_id,
                platform_id,
                commit["sha"],
                "unavailable",
                envelope,
                unavailable_reason="build_failed",
                detail=detail,
            )
            return envelope

        aihc_store: Optional[Path] = None
        aihc_setup_error: Optional[str] = None
        if _requires_installed_store(probe.stdout, probe.stderr):
            aihc_store = root / ".cache" / "aihc-stores" / experiment_id / platform_id / commit["sha"]
            aihc_setup_error = _prepare_aihc_store(
                config,
                platform_id,
                worktree,
                aihc_store,
                float(measurement_config["compile_timeout_seconds"]),
            )

        cells = build_cells(
            config,
            platform_id,
            commit,
            worktree,
            root,
            experiment_id,
            aihc_store=aihc_store,
            aihc_setup_error=aihc_setup_error,
        )
        compiled = compile_cells(cells, root, float(measurement_config["compile_timeout_seconds"]), jobs)
        results = measure_cells(compiled, root, measurement_config)
        envelope = result_envelope(
            experiment_id=experiment_id,
            platform_id=platform_id,
            environment=environment,
            commit=commit,
            compiler_status="available",
            unavailable_reason=None,
            results=results,
            run_id=run_id,
        )
        database.finish_attempt(experiment_id, platform_id, commit["sha"], "complete", envelope)
        return envelope
    except subprocess.TimeoutExpired as error:
        envelope = result_envelope(
            experiment_id=experiment_id,
            platform_id=platform_id,
            environment=environment,
            commit=commit,
            compiler_status="unavailable",
            unavailable_reason="build_failed",
            results=[],
            run_id=run_id,
        )
        database.finish_attempt(
            experiment_id,
            platform_id,
            commit["sha"],
            "unavailable",
            envelope,
            unavailable_reason="build_failed",
            detail=f"compiler build timed out: {error}",
        )
        return envelope
    finally:
        remove_worktree(aihc_repository, worktree)


def build_cells(
    config: Dict[str, Any],
    platform_id: str,
    commit: Dict[str, Any],
    worktree: Path,
    root: Path,
    experiment_id: str,
    *,
    aihc_store: Optional[Path] = None,
    aihc_setup_error: Optional[str] = None,
) -> List[Cell]:
    platform_values = config["platforms"][platform_id]
    cells: List[Cell] = []
    for benchmark in config["benchmarks"]:
        source = (root / benchmark["source"]).resolve()
        for configuration in config["configurations"]:
            version_root = (
                f"ghc-{configuration['compiler_version']}"
                if configuration["compiler_family"] == "ghc"
                else commit["sha"]
            )
            artifact_root = root / ".cache" / "artifacts" / experiment_id / platform_id / version_root
            identity = f"{benchmark['id']}--{configuration['id']}"
            suffix = configuration.get("artifact_suffix", "")
            artifact = artifact_root / identity / f"program{suffix}"
            build_dir = artifact.parent / "build"
            values = {
                "root": str(root),
                "worktree": str(worktree),
                "source": str(source),
                "artifact": str(artifact),
                "build_dir": str(build_dir),
                "commit": commit["sha"],
                **{key: str(value) for key, value in platform_values.items()},
            }
            available = configuration.get("available", True)
            compile_command = expand_command(configuration["compile"], values) if available else None
            if compile_command and configuration["compiler_family"] == "aihc" and aihc_store is not None:
                compile_command.extend(["--store", str(aihc_store)])
            cells.append(
                Cell(
                    benchmark=benchmark,
                    configuration=configuration,
                    commit_sha=commit["sha"],
                    artifact=artifact,
                    build_dir=build_dir,
                    compile_cwd=worktree if configuration["compiler_family"] == "aihc" else root,
                    compile_environment=_compile_environment(configuration),
                    compile_command=compile_command,
                    run_command=expand_command(configuration["run"], values) if available else None,
                    unavailable_reason=None if available else configuration.get("unavailable_reason", "unsupported_configuration"),
                    setup_error=aihc_setup_error if configuration["compiler_family"] == "aihc" else None,
                )
            )
    return cells


def compile_cells(cells: Iterable[Cell], root: Path, timeout_seconds: float, jobs: int) -> List[Tuple[Cell, Dict[str, Any]]]:
    cell_list = list(cells)
    outcomes: List[Tuple[Cell, Dict[str, Any]]] = []
    available = [cell for cell in cell_list if cell.compile_command and not cell.setup_error]
    for cell in cell_list:
        if cell.setup_error:
            outcomes.append((cell, {"status": "compile_failed", "stderr": cell.setup_error}))
        elif not cell.compile_command:
            outcomes.append((cell, {"status": "unavailable", "reason": cell.unavailable_reason}))

    def compile_one(cell: Cell) -> Tuple[Cell, Dict[str, Any]]:
        cell.build_dir.mkdir(parents=True, exist_ok=True)
        cell.artifact.parent.mkdir(parents=True, exist_ok=True)
        if cell.configuration["compiler_family"] == "ghc" and cell.artifact.exists():
            return cell, {"status": "compiled", "artifact_size": cell.artifact.stat().st_size, "cached": True}
        try:
            process = run_command(
                cell.compile_command or [],
                cell.compile_cwd,
                timeout_seconds,
                cell.compile_environment,
            )
        except subprocess.TimeoutExpired:
            return cell, {"status": "compile_timed_out"}
        if process.returncode != 0:
            return cell, {
                "status": "compile_failed",
                "exit_code": process.returncode,
                "stderr": process.stderr[-8192:],
            }
        if not cell.artifact.exists():
            return cell, {"status": "compile_failed", "stderr": "compiler did not create the requested artifact"}
        return cell, {"status": "compiled", "artifact_size": cell.artifact.stat().st_size}

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = [executor.submit(compile_one, cell) for cell in available]
        for future in as_completed(futures):
            outcomes.append(future.result())
    return outcomes


def measure_cells(
    compiled: Iterable[Tuple[Cell, Dict[str, Any]]],
    root: Path,
    measurement_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    ordered = sorted(
        compiled,
        key=lambda item: hashlib.sha256(
            f"{item[0].commit_sha}:{item[0].benchmark['id']}:{item[0].configuration['id']}".encode("utf-8")
        ).digest(),
    )
    for cell, compile_result in ordered:
        base = {
            "benchmark": cell.benchmark["id"],
            "configuration": cell.configuration["id"],
            "compiler_family": cell.configuration["compiler_family"],
            "compiler_version": cell.commit_sha if cell.configuration["compiler_version"] == "commit" else cell.configuration["compiler_version"],
            "backend": cell.configuration["backend"],
            "gc": cell.configuration["gc"],
            "optimization": "O2",
            "compile": compile_result,
        }
        if compile_result["status"] != "compiled":
            base["measurement"] = {"status": "unavailable"}
            results.append(base)
            continue
        measurement = measure_adaptively(
            cell.run_command or [],
            root,
            cell.benchmark["expected_stdout"].encode("utf-8"),
            float(measurement_config["process_timeout_seconds"]),
            float(measurement_config["relative_threshold"]),
            int(measurement_config["maximum_bucket_size"]),
        )
        base["measurement"] = measurement
        results.append(base)
    return results


def _compile_environment(configuration: Dict[str, Any]) -> Dict[str, str]:
    path_variable = configuration.get("compile_path_env")
    if not path_variable:
        return {}
    prefix = os.environ.get(path_variable)
    if not prefix:
        return {}
    environment = {"PATH": f"{prefix}{os.pathsep}{os.environ.get('PATH', '')}"}
    if configuration["compiler_family"] == "aihc" and configuration["backend"] == "wasm":
        environment["AIHC_WASM_CLANG"] = str(Path(prefix) / "clang")
    return environment


def _requires_installed_store(stdout: str, stderr: str) -> bool:
    return "prepare-runtime" in f"{stdout}\n{stderr}"


def _configured_aihc_targets(config: Dict[str, Any], platform_id: str) -> List[Tuple[str, str, Dict[str, str]]]:
    platform_values = config["platforms"][platform_id]
    targets: List[Tuple[str, str, Dict[str, str]]] = []
    seen = set()
    for configuration in config["configurations"]:
        if configuration["compiler_family"] != "aihc" or not configuration.get("available", True):
            continue
        target_template = configuration.get("aihc_target")
        if not target_template:
            continue
        target = target_template.format(**platform_values)
        key = (target, configuration["gc"])
        if key in seen:
            continue
        seen.add(key)
        targets.append((target, configuration["gc"], _compile_environment(configuration)))
    return targets


def _prepare_aihc_store(
    config: Dict[str, Any],
    platform_id: str,
    worktree: Path,
    store: Path,
    timeout_seconds: float,
) -> Optional[str]:
    targets = _configured_aihc_targets(config, platform_id)
    if not targets:
        return None

    store.mkdir(parents=True, exist_ok=True)
    base_command = ["nix", "run", f"{worktree}#aihc", "--"]
    for target, garbage_collector, environment in targets:
        command = base_command + [
            "prepare-runtime",
            "--target",
            target,
            "--gc",
            garbage_collector,
            "--store",
            str(store),
        ]
        error = _run_setup_command(command, worktree, timeout_seconds, environment, "runtime preparation")
        if error:
            return error

    install_environment: Dict[str, str] = {}
    for _, _, environment in targets:
        install_environment.update(environment)
    install_command = base_command + [
        "install",
        str(worktree / "core-libs" / "aihc-base"),
        "--offline",
        "--store",
        str(store),
    ]
    installed_targets = list(dict.fromkeys(target for target, _, _ in targets))
    for target in installed_targets:
        install_command.extend(["--target", target])
    return _run_setup_command(
        install_command,
        worktree,
        timeout_seconds,
        install_environment,
        "library installation",
    )


def _run_setup_command(
    command: List[str],
    cwd: Path,
    timeout_seconds: float,
    environment: Dict[str, str],
    stage: str,
) -> Optional[str]:
    try:
        process = run_command(command, cwd, timeout_seconds, environment)
    except subprocess.TimeoutExpired as error:
        return f"AIHC {stage} timed out: {error}"
    if process.returncode == 0:
        return None
    detail = (process.stderr or process.stdout)[-8192:]
    return f"AIHC {stage} failed with exit code {process.returncode}:\n{detail}"
