from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import expand_command
from .database import Database
from .git_history import GitError, create_worktree, fetch, path_exists, remove_worktree, rev_parse
from .measurement import compile_metrics, measure_adaptively
from .process import run_command, run_measured
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
    stats_file: Optional[str] = None
    stats_format: Optional[str] = None
    run_environment: Dict[str, str] = field(default_factory=dict)
    #: The prepared AIHC store, restored before this cell's timed compile so
    #: that what an earlier cell installed is not already there; see
    #: ``_archive_store``.
    store_archive: Optional[Path] = None


def run_commit(
    *,
    database: Database,
    config: Dict[str, Any],
    experiments: Dict[str, str],
    suite: str,
    platform_id: str,
    machine: Dict[str, Any],
    commit: Dict[str, Any],
    aihc_repository: Path,
    root: Path,
) -> List[Dict[str, Any]]:
    """Measure ``commit`` for the benchmarks in ``experiments``.

    ``experiments`` maps benchmark id to experiment id and lists only the
    benchmarks that still lack a result for this commit. The compiler is
    built and its store prepared once; every benchmark then gets its own
    attempt and envelope under its own experiment, so a benchmark added
    later can be filled in without touching the others. ``suite`` keys the
    shared AIHC store cache. Returns one envelope per experiment.
    """
    machine_id = machine["machine_id"]
    # Before anything is measured, so the whole commit resolves against the
    # index this call holds still.
    hold_hackage_index()
    environment = environment_record(
        platform_id,
        machine.get("derivation", {}).get("cpu_brand", ""),
        hackage_index=hackage_index_identity(),
    )
    run_ids = {benchmark: new_run_id() for benchmark in experiments}
    for benchmark, experiment_id in experiments.items():
        database.start_attempt(experiment_id, platform_id, commit["sha"], run_ids[benchmark], environment, machine_id)
    worktree = root / ".cache" / "aihc-worktree"
    measurement_config = config["measurement"]
    compile_timeout = float(measurement_config["compile_timeout_seconds"])

    def envelope_for(benchmark: str, **fields: Any) -> Dict[str, Any]:
        return result_envelope(
            experiment_id=experiments[benchmark],
            platform_id=platform_id,
            machine_id=machine_id,
            environment=environment,
            commit=commit,
            run_id=run_ids[benchmark],
            benchmark=benchmark,
            **fields,
        )

    def unavailable(reason: str, detail: Optional[str] = None) -> List[Dict[str, Any]]:
        envelopes = []
        for benchmark, experiment_id in experiments.items():
            envelope = envelope_for(benchmark, compiler_status="unavailable", unavailable_reason=reason, results=[])
            database.finish_attempt(
                experiment_id,
                platform_id,
                commit["sha"],
                "unavailable",
                envelope,
                unavailable_reason=reason,
                detail=detail,
            )
            envelopes.append(envelope)
        return envelopes

    if not path_exists(aihc_repository, commit["sha"], config["aihc_compiler_marker"]):
        return unavailable("no_compiler")

    try:
        create_worktree(aihc_repository, worktree, commit["sha"])
        build_error = build_compiler(worktree, root, compile_timeout)
        if build_error is not None:
            return unavailable("build_failed", build_error)

        aihc_store = root / ".cache" / "aihc-stores" / suite / platform_id / commit["sha"]
        aihc_setup_errors = _prepare_aihc_store(config, platform_id, worktree, root, aihc_store, compile_timeout)
        store_archive = _archive_store(aihc_store)

        cells = build_cells(
            config,
            platform_id,
            commit,
            worktree,
            root,
            experiments,
            aihc_store=aihc_store,
            aihc_setup_errors=aihc_setup_errors,
            store_archive=store_archive,
        )
        compiled = compile_cells(cells, root, compile_timeout)
        require_baseline(compiled, experiments)
        results = measure_cells(compiled, root, measurement_config)
        envelopes = []
        for benchmark, experiment_id in experiments.items():
            envelope = envelope_for(
                benchmark,
                compiler_status="available",
                unavailable_reason=None,
                results=[result for result in results if result["benchmark"] == benchmark],
            )
            database.finish_attempt(experiment_id, platform_id, commit["sha"], "complete", envelope)
            envelopes.append(envelope)
        return envelopes
    except subprocess.TimeoutExpired as error:
        return unavailable("build_failed", f"compiler build timed out: {error}")
    finally:
        remove_worktree(aihc_repository, worktree)


#: Where AIHC keeps the package versions it derives from the Hackage index.
#: ``Aihc.Hackage.Cache`` puts it under the XDG cache directory, and
#: ``IndexCache`` appends ``-index`` to that.
def hackage_index_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "aihc" / "hackage-index" / "preferred-versions.txt"


#: Refresh the derived file once it is older than this. Comfortably inside
#: AIHC's own 24h staleness window, so a measured commit always finds a fresh
#: file and never refreshes it itself.
INDEX_WARM_AGE_SECONDS = 12 * 60 * 60


#: The files AIHC derives the index from, beside ``preferred-versions.txt``.
#: ``IndexCache.isStale`` reads the derived table's modification time, and
#: treats a cache without the tarball as incomplete.
INDEX_TABLE_NAME = "index.txt"
INDEX_TARBALL_NAME = "01-index.tar"


def hackage_index_files() -> List[Path]:
    """The cached index files, newest-derived first."""
    directory = hackage_index_cache().parent
    return [directory / INDEX_TABLE_NAME, directory / "preferred-versions.txt"]


def hackage_index_identity() -> Optional[Dict[str, str]]:
    """What the AIHC side resolved against, as a digest of the derived files.

    A result records the machine, the compiler and the benchmark, but nothing
    about the Hackage index that chose its dependency versions -- so two
    machines resolving differently published two numbers that looked
    comparable and were not. The derived table is a few megabytes, so hashing
    it once per commit costs nothing next to a compile.
    """
    digest = hashlib.sha256()
    present = False
    for path in hackage_index_files():
        if not path.is_file():
            continue
        present = True
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    if not present:
        return None
    # Only the digest. The cache's modification time would be the obvious
    # thing to record beside it, but ``hold_hackage_index`` sets that time
    # deliberately, so it would describe the last run rather than the index.
    return {"sha256": digest.hexdigest()[:16]}


def hold_hackage_index() -> None:
    """Keep the cached index from going stale for the length of a run.

    AIHC refetches once the derived table is older than a day. A sweep runs
    for days, so the index would be refreshed partway through -- every commit
    measured after that point resolving against a different Hackage than the
    commits before it, inside one continuous series. Warming before the run
    only moves the moment it happens.

    Touching the table holds the answers still. The pin is deliberate: a run
    measures one index, and picking up a newer one is a decision to make
    between runs, not one to discover in the middle of a series.
    """
    table = hackage_index_cache().parent / INDEX_TABLE_NAME
    tarball = hackage_index_cache().parent / INDEX_TARBALL_NAME
    if not table.is_file() or not tarball.is_file():
        return
    now = time.time()
    os.utime(table, (now, now))


def warm_hackage_index(
    config: Dict[str, Any], platform_id: str, aihc_repository: Path, root: Path, timeout_seconds: float
) -> Optional[str]:
    """Refresh AIHC's Hackage index with a current compiler, before measuring.

    A measured commit that finds the derived file stale refreshes it itself,
    and that refresh is the compiler's own code -- so whether a historical
    commit builds depends on whether it inherited a bug that has since been
    fixed. Commits before ai-haskell-compiler/aihc#2057 retain the whole
    tarball and die with a heap overflow, which would record them as failed
    for no reason but the age of the cache when they happened to be scheduled,
    and a failure is terminal until someone runs ``forget``.

    Refreshing here with ``aihc_ref`` -- the branch under measurement, so the
    newest compiler there is -- means no measured commit ever performs the
    refresh, and every commit in a run resolves against the same index.

    Returns a description when warming fails. That is not fatal: a stale index
    still resolves, and the run should say so rather than stop.
    """
    derived = hackage_index_cache()
    if derived.is_file() and time.time() - derived.stat().st_mtime < INDEX_WARM_AGE_SECONDS:
        return None
    worktree = root / ".cache" / "aihc-index-worktree"
    store = root / ".cache" / "aihc-index-store"
    package = config.get("aihc_index_probe_package", "bytestring")
    target = str(config["platforms"][platform_id]["aihc_native_target"])
    # Fetch first: aihc_ref resolves against the local clone, and a clone that
    # has not been fetched since the fix landed resolves to a compiler that
    # still has the bug -- warming would then build the very compiler it
    # exists to keep away from the refresh. A fetch that fails is not fatal;
    # the ref it already has is still the best available.
    try:
        fetch(aihc_repository)
    except GitError:
        pass
    try:
        head = rev_parse(aihc_repository, config["aihc_ref"])
    except (GitError, KeyError) as error:
        return f"could not resolve {config.get('aihc_ref')}: {error}"
    try:
        create_worktree(aihc_repository, worktree, head)
        build_error = build_compiler(worktree, root, timeout_seconds)
        if build_error is not None:
            return f"the compiler at {head[:12]} does not build:\n{build_error[-2000:]}"
        store.mkdir(parents=True, exist_ok=True)
        command = [
            "nix",
            "run",
            f"{worktree}#aihc",
            "--",
            "install",
            package,
            "--store",
            str(store),
            "--target",
            target,
            "-O2",
            *AIHC_RTS_OPTIONS,
        ]
        process = run_command(command, worktree, timeout_seconds)
        if process.returncode != 0:
            return (process.stdout + process.stderr)[-2000:]
    except subprocess.TimeoutExpired as error:
        return f"warming the Hackage index timed out: {error}"
    finally:
        remove_worktree(aihc_repository, worktree)
    return None


def build_compiler(worktree: Path, root: Path, timeout_seconds: float) -> Optional[str]:
    """Build the commit's compiler, returning the build output when it fails.

    The suite tracks the current AIHC command line only, so a commit whose
    ``aihc`` builds is assumed to offer ``build``, ``install`` and
    ``prepare-runtime`` with the flags ``benchmark.json`` passes them. Commits
    older than ``aihc_since`` predate that command line and are never planned.
    """
    probe = run_command(["nix", "run", f"{worktree}#aihc", "--", "--help"], root, timeout_seconds)
    if probe.returncode != 0:
        return (probe.stderr or probe.stdout)[-8192:]
    return None


class MissingBaseline(RuntimeError):
    """A benchmark produced no baseline binary, so it cannot be compared."""


def require_baseline(compiled: Iterable[Tuple[Cell, Dict[str, Any]]], experiments: Iterable[str]) -> None:
    """Stop the run when a benchmark has no working baseline compiler.

    Every AIHC number is published as a ratio against GHC, so a commit
    measured without a baseline is not a partial result but a useless one --
    and recording it as ``available`` hides a broken machine behind a
    benchmark that merely looks empty. One machine published seventeen
    commits of AIHC-only results this way, and another served a benchmark
    with no GHC series for two days, because a toolchain fault upstream of
    the compile was reported per configuration and never at the run level.

    Treated as non-recoverable: the attempt stays unfinished rather than
    terminal, so the commit is measured again once the machine is fixed
    instead of needing ``forget``.
    """
    baselines: Dict[str, List[Tuple[Cell, Dict[str, Any]]]] = {}
    for cell, outcome in compiled:
        if cell.configuration.get("baseline"):
            baselines.setdefault(cell.benchmark["id"], []).append((cell, outcome))
    for benchmark in experiments:
        outcomes = baselines.get(benchmark, [])
        if any(outcome.get("status") == "compiled" for _, outcome in outcomes):
            continue
        detail = ""
        for cell, outcome in outcomes:
            reported = outcome.get("stderr") or outcome.get("reason") or outcome.get("status", "")
            if reported:
                detail = f"{cell.configuration['id']}: {str(reported).strip().splitlines()[0]}"
                break
        configured = "no baseline configuration ran" if not outcomes else detail
        raise MissingBaseline(f"{benchmark} has no baseline result on this machine ({configured})")



def build_cells(
    config: Dict[str, Any],
    platform_id: str,
    commit: Dict[str, Any],
    worktree: Path,
    root: Path,
    experiments: Dict[str, str],
    *,
    aihc_store: Optional[Path] = None,
    aihc_setup_errors: Optional[Dict[str, str]] = None,
    store_archive: Optional[Path] = None,
) -> List[Cell]:
    aihc_setup_errors = aihc_setup_errors or {}
    platform_values = config["platforms"][platform_id]
    toolchains = os.environ.get("AIHC_BENCH_TOOLCHAINS", "")
    runtime_packaged = runtime_is_package(worktree)
    cells: List[Cell] = []
    for benchmark in config["benchmarks"]:
        if benchmark["id"] not in experiments:
            continue
        experiment_id = experiments[benchmark["id"]]
        source = (root / benchmark["source"]).resolve()
        for configuration in config["configurations"]:
            family = configuration["compiler_family"]
            version_root = f"ghc-{configuration['compiler_version']}" if family == "ghc" else commit["sha"]
            artifact_root = root / ".cache" / "artifacts" / experiment_id / platform_id / version_root
            identity = f"{benchmark['id']}--{configuration['id']}"
            suffix = configuration.get("artifact_suffix", "")
            # ``aihc build`` writes the executables of a Cabal package into a
            # directory, naming each after its own executable stanza, so an
            # AIHC artifact carries the benchmark's package name. The GHC
            # script copies the binary to a path this suite chooses.
            stem = benchmark["package"] if family == "aihc" else "program"
            artifact = artifact_root / identity / f"{stem}{suffix}"
            build_dir = artifact.parent / "build"
            stats_dir = root / ".cache" / "stats" / experiment_id / platform_id / commit["sha"]
            stats_file = stats_dir / f"{identity}.stats"
            corpus = _corpus_directory(benchmark)
            values = {
                "root": str(root),
                "worktree": str(worktree),
                "source": str(source),
                "corpus": corpus,
                "main_file": str(source / "Main.hs"),
                "package": benchmark.get("package", ""),
                "artifact": str(artifact),
                "artifact_dir": str(artifact.parent),
                "build_dir": str(build_dir),
                "commit": commit["sha"],
                "toolchains": toolchains,
                "stats_file": str(stats_file),
                "stats_dir": str(stats_dir),
                **{key: str(value) for key, value in platform_values.items()},
            }
            available = configuration.get("available", True)
            reason: Optional[str] = None if available else configuration.get("unavailable_reason", "unsupported_configuration")
            compile_command = expand_command(configuration["compile"], values) if available else None
            if compile_command and family == "aihc":
                if runtime_packaged:
                    compile_command = _without_gc_option(compile_command)
                if aihc_store is not None:
                    compile_command.extend(["--store", str(aihc_store)])
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
            run_command = expand_command(configuration["run"], values) if available else None
            if run_command is not None and corpus:
                run_command = _with_corpus(run_command, configuration, values)
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
                    run_command=run_command,
                    unavailable_reason=reason,
                    setup_error=setup_error,
                    stats_file=str(stats_file) if available and stats_format else None,
                    stats_format=stats_format if available else None,
                    run_environment=run_environment,
                    store_archive=store_archive if family == "aihc" and available else None,
                )
            )
    return cells


def runtime_is_package(worktree: Path) -> bool:
    """Whether this commit ships its runtime as the ``aihc-rts`` core library.

    Since ai-haskell-compiler/aihc#2142 the runtime is a package that
    ``aihc-prim`` depends on, installed with everything else: there is no
    ``prepare-runtime`` command and ``build`` takes no ``--gc``. Older commits
    have both, and the history is measured across the change, so the runner
    reads the tree rather than assuming either shape.
    """
    return (worktree / "core-libs" / "aihc-rts").is_dir()


def _without_gc_option(command: List[str]) -> List[str]:
    """Drop ``--gc <collector>`` from a compile command."""
    trimmed: List[str] = []
    skip = False
    for part in command:
        if skip:
            skip = False
            continue
        if part == "--gc":
            skip = True
            continue
        trimmed.append(part)
    return trimmed


class MissingCorpus(RuntimeError):
    """A benchmark reads a corpus that this environment does not provide."""


def _corpus_directory(benchmark: Dict[str, Any]) -> str:
    """The directory a corpus benchmark reads, named by an environment variable.

    The flake builds each corpus and exports its store path (for example
    ``AIHC_BENCH_CPP_CORPUS``), the same way it exports the toolchains. A
    benchmark without ``corpus_env`` has no corpus and gets an empty string.
    """
    variable = benchmark.get("corpus_env")
    if not variable:
        return ""
    directory = os.environ.get(variable, "")
    if not directory or not Path(directory).is_dir():
        raise MissingCorpus(
            f"benchmark {benchmark['id']} reads the corpus named by {variable}, which is "
            f"{'unset' if not directory else 'not a directory: ' + directory}; run through the flake so it is built and exported"
        )
    return directory


def _with_corpus(run_command: List[str], configuration: Dict[str, Any], values: Dict[str, str]) -> List[str]:
    """Hand the corpus to the benchmark process.

    The directory becomes the program's last argument. A configuration whose
    program runs under a sandbox names the options that expose a directory in
    ``corpus_options`` (``--dir {corpus}`` for Wasmtime); they go in front of
    the artifact, among the host's own options.
    """
    options = expand_command(configuration.get("corpus_options", []), values)
    try:
        position = run_command.index(values["artifact"])
    except ValueError:
        position = len(run_command)
    return run_command[:position] + options + run_command[position:] + [values["corpus"]]


#: Preparing the runtime and installing the core libraries are compilations
#: too, so aihc gets the whole machine there as well as in the timed step
#: (where ``benchmark.json`` carries the same options).
AIHC_RTS_OPTIONS = ["+RTS", "-N", "-RTS"]


def compile_cells(cells: Iterable[Cell], root: Path, timeout_seconds: float) -> List[Tuple[Cell, Dict[str, Any]]]:
    """Compile every cell, one at a time.

    Compile time is a published metric, so compilation is as timing-sensitive
    as execution and gets the machine to itself. Compiling configurations
    concurrently made compile time a measure of how many other compilers
    happened to be running -- with ``-N`` giving each of them every core, a
    32-core machine defaulted to 32 compilers claiming 32 cores each. The
    compiler still uses the whole machine; only one does at a time.
    """
    cell_list = list(cells)
    outcomes: List[Tuple[Cell, Dict[str, Any]]] = []
    available = [cell for cell in cell_list if cell.compile_command and not cell.setup_error]
    for cell in cell_list:
        if cell.setup_error:
            outcomes.append((cell, {"status": "compile_failed", "stderr": cell.setup_error}))
        elif not cell.compile_command:
            outcomes.append((cell, {"status": "unavailable", "reason": cell.unavailable_reason}))

    def compile_one(cell: Cell) -> Tuple[Cell, Dict[str, Any]]:
        # Compile time is a published metric, so every cell is compiled from
        # scratch: a reused artifact has no compile time at all, and a warm
        # cabal ``dist`` or aihc build directory would time an incremental
        # no-op rather than the compile the metric claims to describe.
        shutil.rmtree(cell.build_dir, ignore_errors=True)
        _restore_store(cell)
        cell.artifact.unlink(missing_ok=True)
        cell.build_dir.mkdir(parents=True, exist_ok=True)
        cell.artifact.parent.mkdir(parents=True, exist_ok=True)
        if cell.stats_file:
            Path(cell.stats_file).parent.mkdir(parents=True, exist_ok=True)
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

    for cell in available:
        outcomes.append(compile_one(cell))
    return outcomes


def _archive_store(store: Path) -> Optional[Path]:
    """Snapshot the AIHC store as preparation left it, for ``_restore_store``.

    ``aihc build`` installs a benchmark's dependencies into the store it is
    given, and that store is shared by every cell of a commit: without a
    snapshot the second benchmark to use ``bytestring`` in a configuration
    would find it already installed and its compile time would not include
    installing it, while GHC's compile time would. Cabal's per-configuration
    store and the wipe in ``compile_one`` give the GHC side the same property
    by simply starting empty.
    """
    if not store.is_dir():
        return None
    archive = store.with_name(f"{store.name}-prepared.tar")
    with tarfile.open(archive, "w") as tar:
        tar.add(store, arcname=".")
    return archive


def _restore_store(cell: Cell) -> None:
    """Put the AIHC store back to what preparation left in it.

    A store records absolute paths, so it is restored to the very path it was
    built at rather than copied per cell.
    """
    if not cell.store_archive or not cell.store_archive.is_file():
        return
    store = cell.store_archive.with_name(cell.store_archive.name[: -len("-prepared.tar")])
    shutil.rmtree(store, ignore_errors=True)
    store.mkdir(parents=True, exist_ok=True)
    with tarfile.open(cell.store_archive, "r") as tar:
        # This suite wrote the archive from a store it had just prepared; the
        # extraction filters exist for archives from elsewhere, and the
        # default becomes a restrictive one in Python 3.14.
        if sys.version_info >= (3, 12):
            tar.extractall(store, filter="fully_trusted")
        else:
            tar.extractall(store)


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
            float(cell.benchmark.get("process_timeout_seconds", measurement_config["process_timeout_seconds"])),
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


def _configured_aihc_builds(config: Dict[str, Any], platform_id: str) -> List[Tuple[str, str, str, Dict[str, str]]]:
    """``(target, gc, optimization, environment)`` for every AIHC configuration.

    An installed package is keyed on its optimization level, so a store entry
    is only reused by the configurations built at the same level.
    """
    platform_values = config["platforms"][platform_id]
    builds: List[Tuple[str, str, str, Dict[str, str]]] = []
    seen = set()
    for configuration in config["configurations"]:
        if configuration["compiler_family"] != "aihc" or not configuration.get("available", True):
            continue
        target_template = configuration.get("aihc_target")
        if not target_template:
            continue
        target = target_template.format(**platform_values)
        key = (target, configuration["gc"], configuration["optimization"])
        if key in seen:
            continue
        seen.add(key)
        builds.append((*key, _compile_environment(configuration)))
    return builds


def _prepare_aihc_store(
    config: Dict[str, Any],
    platform_id: str,
    worktree: Path,
    root: Path,
    store: Path,
    timeout_seconds: float,
) -> Dict[str, str]:
    """Prepare runtimes and install ``aihc-base`` per target.

    ``aihc build`` resolves and installs the dependencies of a benchmark's
    Cabal package itself, inside the timed compile, and that is where they
    belong: installing a dependency is part of what a compiler is being timed
    doing, for ``text`` as much as for ``snappy-hs``. Only ``aihc-base`` is
    installed here -- AIHC's ``base``, the counterpart of the packages GHC
    wires into the compiler, which no benchmark builds either.

    Returns setup errors keyed by target. A target whose runtime or library
    preparation fails does not affect the others, so a missing Wasm sysroot
    leaves the native and LLVM configurations measurable.
    """
    builds = _configured_aihc_builds(config, platform_id)
    errors: Dict[str, str] = {}
    if not builds:
        return errors

    store.mkdir(parents=True, exist_ok=True)
    base_command = ["nix", "run", f"{worktree}#aihc", "--"]
    runtimes: List[Tuple[str, str, Dict[str, str]]] = []
    for target, garbage_collector, _, environment in builds:
        if (target, garbage_collector) not in [(name, gc) for name, gc, _ in runtimes]:
            runtimes.append((target, garbage_collector, environment))
    for target, garbage_collector, environment in runtimes:
        if target in errors or runtime_is_package(worktree):
            continue
        command = base_command + [
            "prepare-runtime",
            "--target",
            target,
            "--gc",
            garbage_collector,
            "--store",
            str(store),
            *AIHC_RTS_OPTIONS,
        ]
        error = _run_setup_command(command, worktree, timeout_seconds, environment, f"runtime preparation for {target}")
        if error:
            errors[target] = error

    # ``aihc-base`` is named by its path in the worktree, which makes it a
    # local package that ``install`` would build in place under the source
    # tree. ``aihc build`` resolves it as a core standin instead and looks for
    # it in the store, so ``--immutable`` is what puts it where the build
    # reads it -- with the identical store key, since that hashes the package
    # and the build, not how it was named.
    core_base = str(worktree / "core-libs" / "aihc-base")
    for target, _, optimization, environment in builds:
        if target in errors:
            continue
        command = base_command + [
            "install",
            core_base,
            "--immutable",
            "--store",
            str(store),
            "--target",
            target,
            f"-{optimization}",
            *AIHC_RTS_OPTIONS,
        ]
        error = _run_setup_command(
            command, worktree, timeout_seconds, environment, f"installation of {core_base} for {target} at -{optimization}"
        )
        if error:
            errors[target] = error
    return errors


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
