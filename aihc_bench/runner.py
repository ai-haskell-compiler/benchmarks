from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .config import CAPABILITIES, OPTIMIZATION_CAPABILITIES, expand_command
from .database import Database
from .freeze import AIHC_IMPLICIT_PACKAGES, parse_build_depends, parse_freeze
from .git_history import create_worktree, path_exists, remove_worktree
from .measurement import compile_metrics, measure_adaptively
from .process import run_command, run_measured
from .schema import environment_record, new_run_id, result_envelope

_OPTIMIZATION_FLAG = re.compile(r"(^|[\s\[])-O(?=[0-3\s,\]]|$)|--optimi[sz]ation|--opt-level")
# The levels ``build-exe --help`` lists for ``-O``, e.g. "Optimization level
# for C sources and LLVM output: 0, 1, 2 or s". optparse-applicative wraps the
# sentence, so the match runs until the default marker or the next option.
_OPTIMIZATION_LEVELS = re.compile(r"optimi[sz]ation level[^:]*:\s*(.*?)(?:\(default|\n\s*-|\n\s*\n|\Z)", re.IGNORECASE | re.DOTALL)


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
    stats_file: Optional[str] = None
    stats_format: Optional[str] = None
    run_environment: Dict[str, str] = field(default_factory=dict)


def run_commit(
    *,
    database: Database,
    config: Dict[str, Any],
    experiment_id: str,
    platform_id: str,
    machine: Dict[str, Any],
    commit: Dict[str, Any],
    aihc_repository: Path,
    root: Path,
    jobs: int,
) -> Dict[str, Any]:
    machine_id = machine["machine_id"]
    environment = environment_record(platform_id, machine.get("derivation", {}).get("cpu_brand", ""))
    run_id = new_run_id()
    database.start_attempt(experiment_id, platform_id, commit["sha"], run_id, environment, machine_id)
    worktree = root / ".cache" / "aihc-worktree"
    measurement_config = config["measurement"]
    compile_timeout = float(measurement_config["compile_timeout_seconds"])

    def unavailable(reason: str, detail: Optional[str] = None, capabilities: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        envelope = result_envelope(
            experiment_id=experiment_id,
            platform_id=platform_id,
            machine_id=machine_id,
            environment=environment,
            commit=commit,
            compiler_status="unavailable",
            unavailable_reason=reason,
            results=[],
            run_id=run_id,
            capabilities=capabilities,
        )
        database.finish_attempt(
            experiment_id,
            platform_id,
            commit["sha"],
            "unavailable",
            envelope,
            unavailable_reason=reason,
            detail=detail,
        )
        return envelope

    if not path_exists(aihc_repository, commit["sha"], config["aihc_compiler_marker"]):
        return unavailable("no_compiler")

    try:
        create_worktree(aihc_repository, worktree, commit["sha"])
        capabilities, probe_error = probe_capabilities(worktree, root, compile_timeout)
        if probe_error is not None:
            return unavailable("build_failed", probe_error)

        aihc_store: Optional[Path] = None
        aihc_setup_errors: Dict[str, str] = {}
        if capabilities["prepare-runtime"]:
            aihc_store = root / ".cache" / "aihc-stores" / experiment_id / platform_id / commit["sha"]
            aihc_setup_errors = _prepare_aihc_store(
                config,
                platform_id,
                worktree,
                root,
                aihc_store,
                compile_timeout,
                capabilities,
            )

        cells = build_cells(
            config,
            platform_id,
            commit,
            worktree,
            root,
            experiment_id,
            aihc_store=aihc_store,
            aihc_setup_errors=aihc_setup_errors,
            capabilities=capabilities,
        )
        compiled = compile_cells(cells, root, compile_timeout, jobs)
        results = measure_cells(compiled, root, measurement_config)
        envelope = result_envelope(
            experiment_id=experiment_id,
            platform_id=platform_id,
            machine_id=machine_id,
            environment=environment,
            commit=commit,
            compiler_status="available",
            unavailable_reason=None,
            results=results,
            run_id=run_id,
            capabilities=capabilities,
        )
        database.finish_attempt(experiment_id, platform_id, commit["sha"], "complete", envelope)
        return envelope
    except subprocess.TimeoutExpired as error:
        return unavailable("build_failed", f"compiler build timed out: {error}")
    finally:
        remove_worktree(aihc_repository, worktree)


def probe_capabilities(worktree: Path, root: Path, timeout_seconds: float) -> Tuple[Dict[str, bool], Optional[str]]:
    """Build the commit's compiler and read its help text for optional features.

    Returns the capability map and, when the compiler cannot be built at all,
    the build output as an error.
    """
    base = ["nix", "run", f"{worktree}#aihc", "--"]
    probe = run_command(base + ["--help"], root, timeout_seconds)
    if probe.returncode != 0:
        return {name: False for name in CAPABILITIES}, (probe.stderr or probe.stdout)[-8192:]
    help_text = f"{probe.stdout}\n{probe.stderr}"
    capabilities = capabilities_from_help(help_text)
    if capabilities["build-exe"]:
        build_help = run_command(base + ["build-exe", "--help"], root, timeout_seconds)
        build_help_text = f"{build_help.stdout}\n{build_help.stderr}"
        capabilities["optimization-flag"] = bool(_OPTIMIZATION_FLAG.search(build_help_text))
        levels = optimization_levels(build_help_text)
        capabilities["optimization-O1"] = "1" in levels
        capabilities["optimization-Os"] = "s" in levels
        capabilities["build-root"] = "--build-root" in build_help_text
    if _mentions_command(help_text, "install"):
        install_help = run_command(base + ["install", "--help"], root, timeout_seconds)
        capabilities["install-offline"] = "--offline" in f"{install_help.stdout}\n{install_help.stderr}"
    return capabilities, None


def capabilities_from_help(help_text: str) -> Dict[str, bool]:
    return {
        "build-exe": _mentions_command(help_text, "build-exe"),
        "compile": _mentions_command(help_text, "compile"),
        "prepare-runtime": _mentions_command(help_text, "prepare-runtime"),
        "install-offline": False,
        "optimization-flag": False,
        "optimization-O1": False,
        "optimization-Os": False,
        "build-root": False,
    }


def optimization_levels(build_help_text: str) -> Set[str]:
    """Return the ``-O`` levels a commit's ``build-exe --help`` advertises."""
    match = _OPTIMIZATION_LEVELS.search(build_help_text)
    if not match:
        return set()
    return set(re.findall(r"(?<![\w-])([0-3]|s)(?!\w)", match.group(1)))


def _mentions_command(help_text: str, command: str) -> bool:
    return re.search(rf"(^|\s){re.escape(command)}(\s|$)", help_text) is not None


def build_cells(
    config: Dict[str, Any],
    platform_id: str,
    commit: Dict[str, Any],
    worktree: Path,
    root: Path,
    experiment_id: str,
    *,
    aihc_store: Optional[Path] = None,
    aihc_setup_errors: Optional[Dict[str, str]] = None,
    capabilities: Optional[Dict[str, bool]] = None,
) -> List[Cell]:
    aihc_setup_errors = aihc_setup_errors or {}
    capabilities = capabilities or {name: name not in OPTIMIZATION_CAPABILITIES for name in CAPABILITIES}
    platform_values = config["platforms"][platform_id]
    build_command = "build-exe" if capabilities.get("build-exe") else "compile"
    toolchains = os.environ.get("AIHC_BENCH_TOOLCHAINS", "")
    cells: List[Cell] = []
    for benchmark in config["benchmarks"]:
        source = (root / benchmark["source"]).resolve()
        for configuration in config["configurations"]:
            family = configuration["compiler_family"]
            version_root = f"ghc-{configuration['compiler_version']}" if family == "ghc" else commit["sha"]
            artifact_root = root / ".cache" / "artifacts" / experiment_id / platform_id / version_root
            identity = f"{benchmark['id']}--{configuration['id']}"
            suffix = configuration.get("artifact_suffix", "")
            artifact = artifact_root / identity / f"program{suffix}"
            build_dir = artifact.parent / "build"
            stats_dir = root / ".cache" / "stats" / experiment_id / platform_id / commit["sha"]
            stats_file = stats_dir / f"{identity}.stats"
            values = {
                "root": str(root),
                "worktree": str(worktree),
                "source": str(source),
                "main_file": str(source / "Main.hs"),
                "package": benchmark.get("package", ""),
                "artifact": str(artifact),
                "build_dir": str(build_dir),
                "commit": commit["sha"],
                "aihc_build_command": build_command,
                "toolchains": toolchains,
                "stats_file": str(stats_file),
                "stats_dir": str(stats_dir),
                **{key: str(value) for key, value in platform_values.items()},
            }
            missing = [name for name in configuration.get("requires", []) if not capabilities.get(name)]
            available = configuration.get("available", True) and not missing
            if not configuration.get("available", True):
                reason: Optional[str] = configuration.get("unavailable_reason", "unsupported_configuration")
            elif missing:
                reason = f"missing_capability:{missing[0]}"
            else:
                reason = None
            compile_command = expand_command(configuration["compile"], values) if available else None
            if compile_command and family == "aihc" and aihc_store is not None:
                compile_command.extend(["--store", str(aihc_store)])
            if compile_command and family == "aihc" and capabilities.get("build-root"):
                # Each cell compiles in its own build root; the default
                # .aihc-target inside the worktree races between parallel cells.
                compile_command.extend(["--build-root", str(build_dir)])
            stats_format = configuration.get("runtime_stats")
            setup_error = None
            if family == "aihc" and available:
                target = str(configuration.get("aihc_target", "")).format(**platform_values)
                setup_error = aihc_setup_errors.get(target) or aihc_setup_errors.get("")
            run_environment: Dict[str, str] = {}
            if available and family == "aihc" and stats_format == "aihc":
                run_environment["AIHC_RTS_STATS"] = str(stats_file)
            cells.append(
                Cell(
                    benchmark=benchmark,
                    configuration=configuration,
                    commit_sha=commit["sha"],
                    artifact=artifact,
                    build_dir=build_dir,
                    compile_cwd=worktree if family == "aihc" else root,
                    compile_environment=_compile_environment(configuration),
                    compile_command=compile_command,
                    run_command=expand_command(configuration["run"], values) if available else None,
                    unavailable_reason=reason,
                    setup_error=setup_error,
                    stats_file=str(stats_file) if available and stats_format else None,
                    stats_format=stats_format if available else None,
                    run_environment=run_environment,
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
        if cell.stats_file:
            Path(cell.stats_file).parent.mkdir(parents=True, exist_ok=True)
        if cell.configuration["compiler_family"] == "ghc" and cell.artifact.exists():
            return cell, {"status": "compiled", "artifact_bytes": cell.artifact.stat().st_size, "cached": True}
        start = time.perf_counter_ns()
        try:
            process = run_command(
                cell.compile_command or [],
                cell.compile_cwd,
                timeout_seconds,
                cell.compile_environment,
            )
        except subprocess.TimeoutExpired:
            return cell, {"status": "compile_timed_out"}
        wall_time_ns = time.perf_counter_ns() - start
        if process.returncode != 0:
            return cell, {
                "status": "compile_failed",
                "exit_code": process.returncode,
                "wall_time_ns": wall_time_ns,
                "stderr": process.stderr[-8192:],
            }
        if not cell.artifact.exists():
            return cell, {"status": "compile_failed", "stderr": "compiler did not create the requested artifact"}
        strip_error = strip_artifact(cell.artifact, root, timeout_seconds)
        if strip_error:
            return cell, {"status": "compile_failed", "wall_time_ns": wall_time_ns, "stderr": strip_error[-8192:]}
        return cell, {
            "status": "compiled",
            "wall_time_ns": wall_time_ns,
            "artifact_bytes": cell.artifact.stat().st_size,
            "stripped": True,
        }

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = [executor.submit(compile_one, cell) for cell in available]
        for future in as_completed(futures):
            outcomes.append(future.result())
    return outcomes


def strip_command(artifact: Path) -> Tuple[List[str], Optional[Path]]:
    """The command that strips ``artifact`` in place, and its temporary output if any.

    Neither compiler strips what it links, so the runner does it before
    recording ``artifact_size``; otherwise the metric mostly compares how much
    debug and symbol information each toolchain happens to emit. ``llvm-strip``
    handles Mach-O and ELF. Wasm needs ``wasm-tools`` because AIHC emits a
    component (``wasm-tools component new``), which neither ``llvm-strip`` nor
    ``wasm-opt`` can parse; ``--all`` also drops ``name`` and ``producers``,
    the sections that make up nearly all of the strippable bytes in both the
    AIHC component and the GHC module. Both tools come from the flake.
    """
    if artifact.suffix == ".wasm":
        stripped = artifact.with_name(artifact.name + ".stripped")
        return ["wasm-tools", "strip", "--all", str(artifact), "-o", str(stripped)], stripped
    return ["llvm-strip", str(artifact)], None


def strip_artifact(artifact: Path, cwd: Path, timeout_seconds: float) -> Optional[str]:
    """Strip ``artifact`` in place; return an error message when that fails."""
    command, stripped = strip_command(artifact)
    try:
        process = run_command(command, cwd, timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"{command[0]} failed: {error}"
    if process.returncode != 0:
        return f"{command[0]} exited with {process.returncode}: {process.stderr or process.stdout}"
    if stripped is not None:
        if not stripped.exists():
            return f"{command[0]} did not write {stripped}"
        os.replace(stripped, artifact)
    return None


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
            "compiler_variant": cell.configuration.get("compiler_variant", "default"),
            "backend": cell.configuration["backend"],
            "gc": cell.configuration["gc"],
            "optimization": cell.configuration["optimization"],
            "baseline": bool(cell.configuration.get("baseline", False)),
            "compile": compile_result,
        }
        if compile_result["status"] != "compiled":
            base["measurement"] = {"status": "unavailable", "reason": compile_result.get("reason", compile_result["status"])}
            results.append(base)
            continue
        invoke = partial(
            run_measured,
            environment_overrides=cell.run_environment,
            stats_file=cell.stats_file,
            stats_format=cell.stats_format,
        )
        measurement = measure_adaptively(
            cell.run_command or [],
            root,
            cell.benchmark["expected_stdout"].encode("utf-8"),
            float(measurement_config["process_timeout_seconds"]),
            float(measurement_config["relative_threshold"]),
            int(measurement_config["maximum_bucket_size"]),
            invoke=invoke,
        )
        if "metrics" in measurement:
            measurement["metrics"].extend(compile_metrics(compile_result))
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
    root: Path,
    store: Path,
    timeout_seconds: float,
    capabilities: Optional[Dict[str, bool]] = None,
) -> Dict[str, str]:
    """Prepare runtimes and install ``aihc-base`` and boot-equivalents per target.

    Returns setup errors keyed by target. A target whose runtime or library
    preparation fails does not affect the others, so a missing Wasm sysroot
    leaves the native and LLVM configurations measurable.
    """
    targets = _configured_aihc_targets(config, platform_id)
    errors: Dict[str, str] = {}
    if not targets:
        return errors

    store.mkdir(parents=True, exist_ok=True)
    base_command = ["nix", "run", f"{worktree}#aihc", "--"]
    prepared: List[Tuple[str, Dict[str, str]]] = []
    for target, garbage_collector, environment in targets:
        if target in errors:
            continue
        command = base_command + [
            "prepare-runtime",
            "--target",
            target,
            "--gc",
            garbage_collector,
            "--store",
            str(store),
        ]
        error = _run_setup_command(command, worktree, timeout_seconds, environment, f"runtime preparation for {target}")
        if error:
            errors[target] = error
        elif target not in [name for name, _ in prepared]:
            prepared.append((target, environment))

    boot_equivalents = _boot_equivalent_dependencies(config, root)
    for target, environment in prepared:
        install_command = base_command + ["install", str(worktree / "core-libs" / "aihc-base")]
        if (capabilities or {}).get("install-offline"):
            install_command.append("--offline")
        install_command.extend(["--store", str(store), "--target", target])
        error = _run_setup_command(install_command, worktree, timeout_seconds, environment, f"library installation for {target}")
        if error:
            errors[target] = error
            continue
        # Only the packages benchmarks actually depend on are installed here
        # (not GHC's full boot set), so a package a future benchmark adds
        # keeps getting picked up automatically without touching this code.
        for name, version in boot_equivalents.items():
            boot_command = base_command + ["install", f"{name}-{version}"]
            if (capabilities or {}).get("install-offline"):
                boot_command.append("--offline")
            boot_command.extend(["--store", str(store), "--target", target])
            error = _run_setup_command(
                boot_command, worktree, timeout_seconds, environment, f"boot library {name} installation for {target}"
            )
            if error:
                errors[target] = error
                break
    return errors


def _boot_equivalent_dependencies(config: Dict[str, Any], root: Path) -> Dict[str, str]:
    """Direct dependencies of any benchmark that GHC ships as a boot library.

    AIHC only stands in for base/ghc-internal/ghc-prim/system-cxx-std-lib/
    template-haskell (see ``aihc_bench.freeze.AIHC_IMPLICIT_PACKAGES``);
    everything else GHC ships for free needs installing into the AIHC store
    ahead of time so it is equally free there, matching GHC's boot set
    (see benchmark.json's ``ghc_boot_libraries``).
    """
    ghc_boot_libraries = config.get("ghc_boot_libraries", {})
    dependencies: Dict[str, str] = {}
    for benchmark in config["benchmarks"]:
        source = (root / benchmark["source"]).resolve()
        if not source.is_dir():
            continue
        freeze_file = source / "cabal.project.freeze"
        cabal_files = list(source.glob("*.cabal"))
        if not freeze_file.is_file() or not cabal_files:
            continue
        pinned = parse_freeze(freeze_file)
        for name in parse_build_depends(cabal_files[0]):
            if name in AIHC_IMPLICIT_PACKAGES or name not in ghc_boot_libraries:
                continue
            dependencies[name] = pinned.get(name, ghc_boot_libraries[name])
    return dependencies


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
