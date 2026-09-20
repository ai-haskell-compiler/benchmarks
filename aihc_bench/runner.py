from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .config import expand_command
from .database import Database
from .git_history import GitError, create_worktree, fetch, path_exists, remove_worktree, rev_parse
from .measurement import MEASURED_STATUSES, compile_metrics, measure_adaptively
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

    phases = Phases()
    try:
        with phases.timing("worktree"):
            create_worktree(aihc_repository, worktree, commit["sha"])
        with phases.timing("compiler_build"):
            build_error = build_compiler(worktree, root, compile_timeout)
        if build_error is not None:
            return unavailable("build_failed", build_error)

        aihc_store = root / ".cache" / "aihc-stores" / suite / platform_id / commit["sha"]
        with phases.timing("aihc_store"):
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
        reused = reusable_baselines(database, config, experiments, platform_id, environment)
        cells = [cell for cell in cells if (cell.benchmark["id"], cell.configuration["id"]) not in reused]
        with phases.timing("compile"):
            compiled = compile_cells(cells, root, compile_timeout)
        # A reused baseline is a baseline: it compiled and ran here, inside
        # the window, on this environment.
        require_baseline(compiled, experiments, satisfied={benchmark for benchmark, _ in reused})
        load_samples: List[float] = []
        with phases.timing("measure"):
            results = measure_cells(compiled, root, measurement_config, load_samples)
        contended = contention_note(load_samples)
        if contended:
            print(f"warning: {contended}", file=sys.stderr)
        results.extend(reused_result(entry) for entry in reused.values())
        for benchmark, reasons in unmeasured_benchmarks(results, experiments).items():
            detail = ", ".join(f"{count} {status}" for status, count in sorted(reasons.items()))
            print(
                f"warning: {benchmark} produced no measurement on any configuration ({detail}); "
                f"it compiles but publishes nothing, so it is absent from the site rather than failing",
                file=sys.stderr,
            )
        envelopes = []
        for benchmark, experiment_id in experiments.items():
            envelope = envelope_for(
                benchmark,
                compiler_status="available",
                unavailable_reason=None,
                results=[result for result in results if result["benchmark"] == benchmark],
                timing={**phases.record(), **({"contended": contended} if contended else {})},
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


#: What a failure looks like in a compiler's output. Searched for before
#: anything else, because the interesting line is rarely the first: taking
#: the first line of stderr reported "Warning: Specifying an absolute path to
#: the project file is deprecated" as the reason a sweep stopped, while the
#: error four lines below said the machine's Hackage index was too old.
_FAILURE = re.compile(r"(^error\b|\berror:|\[Cabal-\d+\]|\bfailed\b|^fatal\b|cannot |could not )", re.IGNORECASE)

#: Preamble a compiler emits before saying anything useful, skipped only when
#: no line looks like a failure at all.
_PREAMBLE = re.compile(r"^(warning\b|note:|resolving dependencies|configuration is affected|--)", re.IGNORECASE)


def first_meaningful_line(text: str) -> str:
    """The line that says what went wrong, not the preamble before it.

    A line that looks like a failure wins wherever it appears. Failing that,
    the first line that is not recognisable preamble; failing that, the first
    line at all, so a message this does not recognise is reported rather than
    swallowed.
    """
    present = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in present:
        if _FAILURE.search(line):
            return line
    for line in present:
        if not _PREAMBLE.match(line):
            return line
    return present[0] if present else ""


class MissingBaseline(RuntimeError):
    """A benchmark produced no baseline binary, so it cannot be compared."""


def require_baseline(
    compiled: Iterable[Tuple[Cell, Dict[str, Any]]],
    experiments: Iterable[str],
    satisfied: Optional[Set[str]] = None,
) -> None:
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
    satisfied = satisfied or set()
    for benchmark in experiments:
        # A reused baseline already compiled and ran on this machine, so the
        # guard has nothing to catch.
        if benchmark in satisfied:
            continue
        outcomes = baselines.get(benchmark, [])
        if any(outcome.get("status") == "compiled" for _, outcome in outcomes):
            continue
        detail = ""
        for cell, outcome in outcomes:
            reported = outcome.get("stderr") or outcome.get("reason") or outcome.get("status", "")
            if reported:
                detail = f"{cell.configuration['id']}: {first_meaningful_line(str(reported))}"
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


class Phases:
    """Wall clock per phase of a commit, so a slow commit can be explained.

    A commit's cost was only ever visible per cell: compile and run times are
    recorded, but building the compiler and preparing its stores -- which
    happen once per commit and need no benchmark -- were not, so the largest
    parts of an hour-long commit left no trace. A budget cannot be held
    against what is not measured.
    """

    def __init__(self) -> None:
        self.elapsed_ns: Dict[str, int] = {}

    @contextmanager
    def timing(self, name: str):
        start = time.perf_counter_ns()
        try:
            yield
        finally:
            self.elapsed_ns[name] = self.elapsed_ns.get(name, 0) + (time.perf_counter_ns() - start)

    def record(self) -> Dict[str, Any]:
        total = sum(self.elapsed_ns.values())
        return {"phases_ns": dict(self.elapsed_ns), "total_ns": total}


#: How long a GHC baseline may be reused before it is measured again.
#: Overridden by ``baseline_reuse_hours`` in ``benchmark.json``; zero measures
#: every configuration on every commit.
DEFAULT_BASELINE_REUSE_HOURS = 24.0


def unmeasured_benchmarks(results: List[Dict[str, Any]], benchmarks: Iterable[str]) -> Dict[str, Dict[str, int]]:
    """Benchmarks whose every cell compiled but none produced a measurement.

    Such a benchmark is not visible as a failure anywhere. Its run is
    recorded ``available`` -- the compiler worked -- and it simply carries no
    measurements, so the site has nothing to plot and the benchmark quietly
    disappears from a suite it is still nominally part of.

    ``aihc-cpp-stackage`` sat like that from the day it was added: every cell
    built and ran, every one failed ``validation_failed`` against an expected
    tally that was wrong, and nothing said so.
    """
    silent: Dict[str, Dict[str, int]] = {}
    for benchmark in benchmarks:
        entries = [result for result in results if result["benchmark"] == benchmark]
        if not entries:
            continue
        if any((entry.get("measurement") or {}).get("status") in MEASURED_STATUSES for entry in entries):
            continue
        reasons: Dict[str, int] = {}
        for entry in entries:
            status = (entry.get("measurement") or {}).get("status") or "unknown"
            reasons[status] = reasons.get(status, 0) + 1
        silent[benchmark] = reasons
    return silent


def reusable_baselines(
    database: Database,
    config: Dict[str, Any],
    experiments: Dict[str, str],
    platform_id: str,
    environment: Dict[str, Any],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """GHC results recent enough to stand in for this commit's, by cell.

    A GHC configuration's inputs are the benchmark, the toolchain and the
    machine. None of them is the AIHC commit under test, so measuring GHC
    again for every AIHC commit re-derives a number that cannot have moved:
    on one machine that was 190 seconds a commit across three benchmarks,
    against an AIHC side that is the only thing being asked a question.

    What does move is the machine. A reused baseline is a number from a
    different moment, so the ratio published against it no longer has drift
    cancelling on both sides; the window is what bounds that, and a changed
    ``environment_id`` -- a new OS, CPU or runner -- discards the lot.

    Only a measured, successful GHC result is reused. A failure is a question
    about this machine now, and the AIHC side is never reused at all: its
    compiler is the commit.
    """
    hours = float(config.get("baseline_reuse_hours", DEFAULT_BASELINE_REUSE_HOURS))
    if hours <= 0:
        return {}
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600))
    reusable: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for benchmark, experiment_id in experiments.items():
        for entry in database.results_measured_since(experiment_id, platform_id, environment.get("id", ""), since):
            if entry.get("compiler_family") != "ghc":
                continue
            if (entry.get("measurement") or {}).get("status") not in MEASURED_STATUSES:
                continue
            reusable[(benchmark, entry["configuration"])] = entry
    return reusable


def reused_result(entry: Dict[str, Any]) -> Dict[str, Any]:
    """A reused entry, saying so and saying where it came from."""
    result = {key: value for key, value in entry.items() if not key.startswith("_")}
    result["reused_from"] = {
        "commit_sha": entry.get("_measured_for"),
        "measured_at": entry.get("_measured_at"),
    }
    return result


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
    load_samples: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Measure every compiled cell, appending the load seen before each one.

    Measurement is sequential, so the load while it runs says whether
    anything else had the machine at the same time.
    """
    results: List[Dict[str, Any]] = []
    ordered = sorted(
        compiled,
        key=lambda item: hashlib.sha256(
            f"{item[0].commit_sha}:{item[0].benchmark['id']}:{item[0].configuration['id']}".encode("utf-8")
        ).digest(),
    )
    for cell, compile_result in ordered:
        if load_samples is not None:
            load = machine_load()
            if load is not None:
                load_samples.append(load)
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
            cell_budget_seconds=float(measurement_config.get("cell_budget_seconds", 0.0)),
        )
        if "metrics" in measurement:
            measurement["metrics"].extend(compile_metrics(compile_result))
        base["measurement"] = measurement
        results.append(base)
    return results


#: Load average above which the machine is taken to be shared. Measurement is
#: sequential by design -- one process at a time, nothing else the suite does
#: runs beside it -- so the load while measuring should sit near one. Two
#: leaves room for the runner itself and a idle desktop.
CONTENDED_LOAD = 2.0


def machine_load() -> Optional[float]:
    """One-minute load average, or ``None`` where the platform has none."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def contention_note(samples: Optional[List[float]]) -> Optional[str]:
    """Say when a measurement shared the machine, or nothing when it did not.

    Compile time and run time are both published, and both move under
    contention in a way that is indistinguishable afterwards from a change in
    the compiler. The suite runs its measurements sequentially precisely so
    that nothing of its own competes -- but nothing stopped a second runner,
    an editor's language server or another build from doing so, and both
    happened on these machines: a second sweep started beside the continuous
    service, and a laptop measured while other work compiled on it.

    Detecting it does not make the numbers good. It makes them answerable.
    """
    if not samples:
        return None
    # The median, not the peak. Load average is a trailing one-minute mean and
    # the compile phase before this one uses every core, so the first samples
    # carry the decay of the suite's own work: taking the peak reported
    # contention on an idle machine every time. Something that really shares
    # the machine is there for the whole measurement, so it moves the median.
    ordered = sorted(samples)
    median = ordered[len(ordered) // 2] if len(ordered) % 2 else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
    if median <= CONTENDED_LOAD:
        return None
    return (
        f"the machine was not idle while measuring (median one-minute load {median:.1f}, "
        f"above {CONTENDED_LOAD:.1f}); timings on this commit share the machine with something else"
    )


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
    setup_notes: Dict[str, List[str]] = {}
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
    for target, _, optimization, environment in builds:
        if target in errors:
            continue
        for package in core_library_paths(worktree):
            command = base_command + [
                "install",
                str(package),
                "--immutable",
                "--store",
                str(store),
                "--target",
                target,
                f"-{optimization}",
                *AIHC_RTS_OPTIONS,
            ]
            error = _run_setup_command(
                command,
                worktree,
                timeout_seconds,
                environment,
                f"installation of {package} for {target} at -{optimization}",
            )
            if error and package.name == CORE_BASE_PACKAGE:
                # Without aihc-base nothing on this target builds at all.
                errors[target] = error
                break
            if error:
                # The timed compile builds it instead, as it did before this
                # was prepared: slower and not comparable, but measurable.
                setup_notes.setdefault(target, []).append(error.splitlines()[0])
    return errors


#: AIHC's counterpart of the packages GHC wires into the compiler. GHC never
#: rebuilds ``base``, ``ghc-internal``, ``ghc-prim``, ``rts`` or
#: ``template-haskell`` for a benchmark -- ``WIRED_IN_PACKAGES`` in
#: ``compile_with_cabal`` keeps them installed -- and AIHC's own lock marks
#: the same set ``"source": "core"``.
#:
#: Only ``aihc-base`` used to be prepared, which put the rest inside the timed
#: compile: a benchmark reaching ``bytestring`` pulled in ``aihc-internal``
#: and ``aihc-template-haskell``, about 11 MB of core library, and rebuilt
#: them for every cell. AIHC was charged for work GHC gets free, and charged
#: for it twelve times a commit.
CORE_BASE_PACKAGE = "aihc-base"


def core_library_paths(worktree: Path) -> List[Path]:
    """The core libraries to prepare, ``aihc-base`` first.

    Read from the tree rather than listed here, since the set moves with the
    compiler: ``aihc-rts`` became one of them in ai-haskell-compiler/aihc#2142.
    """
    directory = worktree / "core-libs"
    if not directory.is_dir():
        return []
    packages = sorted(path for path in directory.iterdir() if (path / f"{path.name}.cabal").is_file())
    packages.sort(key=lambda path: path.name != CORE_BASE_PACKAGE)
    return packages


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
