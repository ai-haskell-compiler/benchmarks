from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import counters
from .compare import CompareError, format_report, resolve_side, run_compare, select_configuration, worktree_side
from .config import OPTIMIZATION_PROFILES, ConfigError, detect_platform, experiment_ids, load_config, suite_key
from .database import Database
from .git_history import DEFAULT_REMOTE, GitError, clone, clone_directory, commits, fetch, is_remote
from .machine import load_machine
from .planner import build_plan, merge_terminal_attempts
from .process import raise_open_files_limit, run_command, utf8_locale
from .cabal_index import index_state_reached, newest_freeze_index_state, package_list, refresh_package_list
from .runner import hackage_index_cache, run_commit, warm_hackage_index
from .schedule import Schedule, Window
from .toolchain import MachineFault, pin_runner_environment
from .uploader import UploadError, check_login, refresh_overview, upload_pending
from .watch import RunBusy, Watcher, run_lock


_REPO_HELP = f"AIHC checkout or clone URL; defaults to {DEFAULT_REMOTE}"


def main(argv: Optional[list] = None) -> None:
    parser = _parser()
    arguments = parser.parse_args(argv)
    root = Path(arguments.root).resolve()
    # Before anything is spawned: every compiler and measured program
    # inherits the limit. See process.OPEN_FILES_LIMIT.
    raise_open_files_limit()
    try:
        config = load_config((root / arguments.config).resolve())
        platform_id = arguments.platform or detect_platform()
        if platform_id not in config["platforms"]:
            raise ConfigError(f"platform {platform_id!r} is not configured")
        experiments = experiment_ids(config)
        suite = suite_key(config)
        state_path = (root / arguments.state).resolve()
        machine = load_machine(state_path.parent, getattr(arguments, "machine", None))
        database = Database(state_path)
        try:
            _dispatch(arguments, root, config, platform_id, experiments, suite, database, machine)
        finally:
            database.close()
    except MachineFault as error:
        # A machine fault, not a result: say so and stop rather than filling the
        # history with what the machine got wrong. Nothing of the commit was
        # recorded, so the next run measures it again.
        print(f"error: {error}", file=sys.stderr)
        print("this machine needs fixing before it measures again; run `doctor`", file=sys.stderr)
        raise SystemExit(2) from error
    except RunBusy as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(3) from error
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (CompareError, ConfigError, GitError, UploadError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")


def _dispatch(
    arguments: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    platform_id: str,
    experiments: Dict[str, str],
    suite: str,
    database: Database,
    machine: Dict[str, Any],
) -> None:
    if arguments.command == "doctor":
        _doctor(arguments, root, config, platform_id, experiments, suite, machine)
        return
    if arguments.command == "compare":
        repository = _repository(arguments, root)
        if arguments.b is None and not arguments.worktree:
            raise ValueError("give a second commit or --worktree PATH")
        if arguments.b is not None and arguments.worktree:
            raise ValueError("--worktree replaces the second commit; give one or the other")
        cache = root / ".cache" / "compare"
        side_a = resolve_side(repository, arguments.a, cache)
        side_b = worktree_side(Path(arguments.worktree)) if arguments.worktree else resolve_side(repository, arguments.b, cache)
        selected = select_configuration(config, benchmarks=arguments.bench, configurations=arguments.config_ids, profile=arguments.profile)
        report = run_compare(
            config=selected,
            platform_id=platform_id,
            root=root,
            aihc_repository=repository,
            sides=(side_a, side_b),
            rounds=arguments.rounds,
            log=lambda message: print(message, file=sys.stderr),
        )
        database.record_adhoc(report)
        print(format_report(report, markdown=arguments.markdown))
        return

    if arguments.command == "window":
        _window(arguments, Schedule(database.path.parent / "schedule.json"))
        return

    if arguments.command == "watch":
        _watch(arguments, root, config, platform_id, experiments, database, machine)
        return

    if arguments.command == "upload":
        if not arguments.dry_run:
            check_login(config, root)
        summary = upload_pending(
            database, experiments, suite, platform_id, config, root, database.commits(), dry_run=arguments.dry_run, limit=arguments.limit
        )
        print(f"uploaded {summary['uploaded']} runs ({summary['pending']} still pending)")
        if summary["uploaded"]:
            refresh_overview(config)
        return

    if arguments.command == "plan":
        repository = _repository(arguments, root)
        history = _refresh_history(database, config, repository, platform_id, experiments, arguments.fetch)
        _print_plan(database, experiments, suite, platform_id, len(history))
        return

    if arguments.command == "run":
        # Before the history is touched: a second run on the machine would
        # measure the first one's load. See watch.run_lock.
        with run_lock(database.path.parent / "run.lock"):
            _run(arguments, root, config, platform_id, experiments, suite, database, machine)
        return

    if arguments.command == "forget":
        sha = _resolve_commit(database, arguments.commit)
        forgotten = [benchmark for benchmark, experiment in experiments.items() if database.forget(experiment, platform_id, sha)]
        if forgotten:
            print(f"forgot active results for {sha}: {', '.join(forgotten)}")
        else:
            raise ValueError(f"no active result for {sha}")
        return

    raise ValueError(f"unsupported command {arguments.command}")


def _run(
    arguments: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    platform_id: str,
    experiments: Dict[str, str],
    suite: str,
    database: Database,
    machine: Dict[str, Any],
) -> None:
    repository = _repository(arguments, root)
    history = _refresh_history(database, config, repository, platform_id, experiments, arguments.fetch)
    if arguments.upload:
        check_login(config, root)
    # Refresh both package lists before any commit is measured: cabal's,
    # so a freeze pin newer than the machine's last `cabal update` does
    # not take out every GHC baseline of a benchmark (see
    # cabal_index.refresh_package_list), and AIHC's, so no measured
    # commit performs the refresh itself (see warm_hackage_index).
    list_error = refresh_package_list(config, root, float(config["measurement"]["compile_timeout_seconds"]))
    if list_error:
        print(f"warning: cabal's package list is behind, GHC baselines may not resolve: {list_error.splitlines()[0]}")
    index_error = warm_hackage_index(config, platform_id, repository, root, float(config["measurement"]["compile_timeout_seconds"]))
    if index_error:
        print(f"warning: could not warm the Hackage index, measuring against whatever is cached: {index_error.splitlines()[0]}")
    completed = 0
    while True:
        # Before choosing, not only before the sweep. A sweep runs for
        # hours and the branch moves while it does, so a run that planned
        # once kept measuring an old history and never saw the commit that
        # had just landed -- the one most worth measuring.
        if completed:
            history = _refresh_history(database, config, repository, platform_id, experiments, arguments.fetch)
        by_experiment = _terminal_by_experiment(database, experiments, platform_id)
        plan = build_plan(database.commits(), merge_terminal_attempts(by_experiment))
        next_commit = plan["next"]
        if not next_commit:
            print("all commits have terminal results")
            break
        # Only the benchmarks without a result for this commit are measured,
        # so a benchmark added later fills in without re-measuring the rest.
        missing = {
            benchmark: experiment
            for benchmark, experiment in experiments.items()
            if next_commit["sha"] not in {attempt["commit_sha"] for attempt in by_experiment[experiment]}
        }
        print(
            f"benchmarking {next_commit['sha'][:12]} ({next_commit['ordinal'] + 1}/{len(history)}, {plan['stage']}, "
            f"{', '.join(missing)}): {next_commit['subject']}"
        )
        envelopes = run_commit(
            database=database,
            config=config,
            experiments=missing,
            suite=suite,
            platform_id=platform_id,
            machine=machine,
            commit=next_commit,
            aihc_repository=repository,
            root=root,
            )
        for envelope in envelopes:
            print(f"recorded {envelope['compiler_status']} result {envelope['run_id']} for {envelope['benchmark']}")
        _report_commit_timing(config, envelopes)
        inherited = sum(database.propagate_inherited(experiment, platform_id) for experiment in missing.values())
        if inherited:
            print(f"propagated the result to {inherited} same-tree benchmark results")
        if arguments.upload:
            _upload_after_commit(database, experiments, suite, platform_id, config, root)
        completed += 1
        if not arguments.all or (arguments.limit and completed >= arguments.limit):
            break


def _terminal_by_experiment(database: Database, experiments: Dict[str, str], platform_id: str) -> Dict[str, list]:
    return {experiment: database.terminal_attempts(experiment, platform_id) for experiment in experiments.values()}


def _print_experiments(experiments: Dict[str, str], suite: str) -> None:
    print(f"suite:      {suite}")
    for benchmark, experiment in experiments.items():
        print(f"experiment: {experiment}  ({benchmark})")


def _print_plan(database: Database, experiments: Dict[str, str], suite: str, platform_id: str, total: int) -> None:
    by_experiment = _terminal_by_experiment(database, experiments, platform_id)
    terminal = merge_terminal_attempts(by_experiment)
    plan = build_plan(database.commits(), terminal)
    measured = sum(1 for attempt in terminal if not attempt.get("inherited_from"))
    inherited = len(terminal) - measured
    _print_experiments(experiments, suite)
    print(f"platform:   {platform_id}")
    print(f"complete:   {len(terminal)}/{total} ({measured} measured, {inherited} inherited)")
    for benchmark, experiment in experiments.items():
        covered = len(by_experiment[experiment])
        if covered != len(terminal):
            print(f"            {benchmark}: {covered}/{total}")
    next_commit = plan["next"]
    if next_commit:
        print(f"next:       {next_commit['sha']}  {next_commit['subject']}  [{plan['stage']}]")
    else:
        print("next:       none")
    if plan["gaps"]:
        print("gaps:       ordinals      width  signal")
        for gap in plan["gaps"][:5]:
            span = f"{gap['start']['ordinal'] + 1}-{gap['end']['ordinal'] + 1}"
            print(f"            {span:12} {gap['width']:5}  {gap['signal']:.3f}")


#: A commit that takes longer than this is reported. The suite exists to
#: measure a compiler's history, and a history is only measurable at a rate
#: that keeps up with it.
DEFAULT_COMMIT_BUDGET_SECONDS = 3600.0


def _refresh_history(
    database: Database,
    config: Dict[str, Any],
    repository: Path,
    platform_id: str,
    experiments: Dict[str, str],
    fetch_first: bool,
) -> List[Dict[str, Any]]:
    """Reload the commit history, fetching it first when asked.

    A fetch that fails leaves the history as it was and says so. The branch
    being briefly unreachable is not a reason to stop a sweep that has hours
    of measuring left and a perfectly good list of commits already.
    """
    if fetch_first:
        try:
            fetch(repository)
        except GitError as error:
            print(f"warning: could not fetch the compiler history, planning against what is already cloned: {error}")
    history = commits(
        repository, config.get("aihc_ref", "origin/main"), config["aihc_tree_paths"], since=config.get("aihc_since")
    )
    database.replace_commits(history)
    for experiment in experiments.values():
        database.propagate_inherited(experiment, platform_id)
    return history


def _report_commit_timing(config: Dict[str, Any], envelopes: List[Dict[str, Any]]) -> None:
    """Say where a commit's wall clock went, and whether it fits the budget.

    Every envelope of a commit carries the same timing record: the phases
    happen once for the commit, not once per benchmark.
    """
    timing = next((envelope.get("timing") for envelope in envelopes if envelope.get("timing")), None)
    if not timing:
        return
    phases = timing.get("phases_ns") or {}
    total = timing.get("total_ns") or sum(phases.values())
    if not total:
        return
    parts = ", ".join(
        f"{name} {phases[name] / 1e9:.0f}s"
        for name in sorted(phases, key=lambda name: phases[name], reverse=True)
        if phases[name]
    )
    print(f"commit took {total / 1e9 / 60:.0f} min: {parts}")
    budget = float(config.get("commit_budget_seconds", DEFAULT_COMMIT_BUDGET_SECONDS))
    if budget > 0 and total / 1e9 > budget:
        print(
            f"warning: the commit took {total / 1e9 / 60:.0f} min against a "
            f"{budget / 60:.0f} min budget; the history is measured slower than it grows"
        )


def _upload_after_commit(
    database: Database,
    experiments: Dict[str, str],
    suite: str,
    platform_id: str,
    config: Dict[str, Any],
    root: Path,
) -> None:
    """Upload what is pending, reporting a failure rather than ending the run.

    The measurements are already recorded and stay pending, so the next
    commit's upload retries them and ``upload`` catches up afterwards. A
    transient Cloudflare API error otherwise threw away every commit still to
    be measured: three long runs died this way, each on a "wrangler d1 execute
    failed: Authentication error [code: 10000]" that succeeded again minutes
    later.
    """
    try:
        summary = upload_pending(database, experiments, suite, platform_id, config, root, database.commits())
        print(f"uploaded {summary['uploaded']} runs ({summary['pending']} still pending)")
        if summary["uploaded"]:
            refresh_overview(config)
    except UploadError as error:
        print(f"warning: upload failed, results stay pending and will be retried: {str(error).splitlines()[0]}")


def _doctor(
    arguments: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    platform_id: str,
    experiments: Dict[str, str],
    suite: str,
    machine: Dict[str, Any],
) -> None:
    repository = _repository(arguments, root)
    failures = []
    derivation = machine.get("derivation", {})
    _print_experiments(experiments, suite)
    print(f"platform:   {platform_id}")
    print(f"machine:    {machine['machine_id']}")
    print(f"cpu:        {derivation.get('cpu_brand') or 'unknown'}")
    print(f"identity:   {derivation.get('identifier_source', 'unknown')}{' (overridden)' if derivation.get('overridden') else ''}")
    if derivation.get("identifier_source") == "hostname":
        print("warning:    no hardware identifier was readable; the machine id is derived from the hostname")
    print(f"aihc repo:  {repository}")
    for executable in ("git", "nix", "wrangler"):
        resolved = shutil.which(executable)
        print(f"{executable:10} {resolved or 'missing'}")
        if not resolved:
            failures.append(executable)
    toolchains = os.environ.get("AIHC_BENCH_TOOLCHAINS")
    print(f"toolchains: {toolchains or 'missing (run through the flake so AIHC_BENCH_TOOLCHAINS is set)'}")
    if not toolchains or not (Path(toolchains) / "bin").is_dir():
        failures.append("toolchains")
    for variable in sorted({benchmark["corpus_env"] for benchmark in config["benchmarks"] if benchmark.get("corpus_env")}):
        corpus = os.environ.get(variable)
        print(f"corpus:     {variable}={corpus or 'missing (run through the flake so it is built and exported)'}")
        if not corpus or not Path(corpus).is_dir():
            failures.append(variable)
    # The same check ``run`` makes before every commit, so a machine fault it
    # stops on can be reproduced and confirmed fixed here.
    try:
        pinned = pin_runner_environment(root, float(config["measurement"]["compile_timeout_seconds"]))
        print(f"tools:      {len(pinned)} store paths rooted and verified")
    except MachineFault as error:
        print(f"tools:      {error}")
        failures.append("runner tools")
    locale_name = utf8_locale()
    print(f"locale:     {locale_name or 'missing (no UTF-8 locale is supported; aihc cannot write its core files)'}")
    if not locale_name:
        failures.append("utf8 locale")
    # Not a failure: without counters the instruction and cycle metrics are
    # recorded unavailable and everything else is measured as before.
    print(f"counters:   {counters.probe()}")
    derived = hackage_index_cache()
    if derived.is_file():
        age = (time.time() - derived.stat().st_mtime) / 3600
        print(f"aihc index: {derived} ({age:.1f}h old)")
    else:
        print(f"aihc index: missing ({derived}); run will fetch it before measuring")
    index = package_list(root)
    if index is None:
        print("hackage:    missing (could not ask cabal for its cache directory)")
        failures.append("hackage index")
    elif not index.is_file() or index.stat().st_size == 0:
        print(f"hackage:    missing (no package list at {index}; run 'cabal update')")
        failures.append("hackage index")
    else:
        wanted, benchmark = newest_freeze_index_state(config, root)
        reached = index_state_reached(index)
        if wanted and reached and reached < wanted:
            print(
                f"hackage:    {index}\n"
                f"            stale: {benchmark} pins index-state {wanted}, this machine reaches "
                f"{reached}; run 'cabal update'"
            )
            failures.append("hackage index older than a benchmark's index-state")
        else:
            print(f"hackage:    {index}" + (f" (covers index-state {wanted})" if wanted else ""))
    if not (repository / ".git").exists() and not (repository / "HEAD").exists():
        failures.append("aihc repository")
        print("repository does not appear to be a Git checkout")
    print(f"server:     {config['publishing']['server_url']}")
    try:
        print(f"cloudflare: {check_login(config, root)}")
    except UploadError as error:
        print(f"cloudflare: {error}")
        failures.append("wrangler login")
    if failures:
        raise ValueError("doctor found missing requirements: " + ", ".join(failures))


def _window(arguments: argparse.Namespace, schedule: Schedule) -> None:
    if arguments.action == "add":
        windows = schedule.add(Window.parse(_window_spec(arguments)))
    elif arguments.action == "remove":
        windows = schedule.remove(Window.parse(_window_spec(arguments)))
    elif arguments.action == "clear":
        schedule.save([])
        windows = []
    else:
        windows = schedule.windows()
    if windows:
        print(f"benchmarks start only within {', '.join(map(str, windows))}, local time")
    else:
        print("benchmarks start at any time")


def _window_spec(arguments: argparse.Namespace) -> str:
    if not arguments.spec:
        raise ValueError(f"window {arguments.action} needs a window such as 22:00-06:00")
    return arguments.spec


def _watch(
    arguments: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    platform_id: str,
    experiments: Dict[str, str],
    database: Database,
    machine: Dict[str, Any],
) -> None:
    repository = _repository(arguments, root)
    forwarded = ["--root", str(root), "--config", arguments.config, "--state", arguments.state]
    if arguments.platform:
        forwarded += ["--platform", arguments.platform]
    command = [sys.executable, "-m", "aihc_bench", *forwarded, "run", "--fetch", "--aihc-repo", str(repository)]
    if not arguments.no_upload:
        command.append("--upload")
    watcher = Watcher(
        root=root,
        config=config,
        database=database,
        experiments=experiments,
        platform_id=platform_id,
        machine_id=machine["machine_id"],
        refresh_history=lambda fetch_first: _refresh_history(database, config, repository, platform_id, experiments, fetch_first),
        run_command=command,
        # Through the flake again, so a changed flake.nix or flake.lock is
        # evaluated afresh along with the code.
        restart_command=["nix", "run", str(root), "--", *sys.argv[1:]],
        poll_seconds=arguments.poll,
        allow_battery=arguments.allow_battery,
    )
    watcher.loop(once=arguments.once)


def _repository(arguments: argparse.Namespace, root: Path) -> Path:
    """Resolve the AIHC checkout to benchmark.

    ``--aihc-repo`` and ``AIHC_REPOSITORY`` take either a local checkout or a
    clone URL; without them the upstream repository is used, cloned once into
    ``.cache/aihc`` and reused afterwards.
    """
    value = arguments.aihc_repo or os.environ.get("AIHC_REPOSITORY") or DEFAULT_REMOTE
    if is_remote(value):
        return clone(value, clone_directory(root / ".cache" / "aihc", value))
    repository = Path(value).expanduser().resolve()
    if not repository.exists():
        raise ValueError(f"AIHC repository does not exist: {repository}")
    return repository


def _resolve_commit(database: Database, prefix: str) -> str:
    matches = [commit["sha"] for commit in database.commits() if commit["sha"].startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"commit prefix {prefix!r} matched {len(matches)} commits")
    return matches[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aihc-bench")
    parser.add_argument("--root", default=".", help=argparse.SUPPRESS)
    parser.add_argument("--config", default="benchmark.json")
    parser.add_argument("--state", default=".state/benchmarks.sqlite3")
    parser.add_argument("--platform", choices=["aarch64-darwin", "x86_64-linux"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate the local environment and print the machine id")
    doctor.add_argument("--aihc-repo", help=_REPO_HELP)
    doctor.add_argument("--machine", help="override and freeze the derived machine id")

    plan = subparsers.add_parser("plan", help="show coverage, the next commit, and the highest-scoring gaps")
    plan.add_argument("--aihc-repo", help=_REPO_HELP)
    plan.add_argument("--fetch", action="store_true")

    run = subparsers.add_parser("run", help="benchmark the next commit")
    run.add_argument("--aihc-repo", help=_REPO_HELP)
    run.add_argument("--fetch", action="store_true")
    run.add_argument("--all", action="store_true", help="continue until all commits are terminal")
    run.add_argument("--limit", type=int, default=0, help="maximum commits for --all; zero means unlimited")
    run.add_argument("--upload", action="store_true", help="upload results to the Worker after each commit")

    compare = subparsers.add_parser("compare", help="benchmark two AIHC builds against each other, locally")
    compare.add_argument("a", help="commit, branch or tag for side A")
    compare.add_argument("b", nargs="?", help="commit, branch or tag for side B")
    compare.add_argument("--worktree", help="use this AIHC checkout, including uncommitted changes, as side B")
    compare.add_argument("--aihc-repo", help=_REPO_HELP)
    compare.add_argument("--bench", action="append", default=[], metavar="ID", help="benchmark id; repeatable, default all")
    compare.add_argument("--config", dest="config_ids", action="append", default=[], metavar="ID", help="configuration id; repeatable, default every AIHC configuration")
    compare.add_argument("--profile", choices=list(OPTIMIZATION_PROFILES))
    compare.add_argument("--rounds", type=int, default=10, help="interleaved A/B rounds per cell")
    compare.add_argument("--markdown", action="store_true", help="print a Markdown table")

    watch = subparsers.add_parser("watch", help="benchmark commit after commit, explaining each choice (what ./bench runs)")
    watch.add_argument("--aihc-repo", help=_REPO_HELP)
    watch.add_argument("--no-upload", action="store_true", help="keep results local instead of uploading after each commit")
    watch.add_argument("--allow-battery", action="store_true", help="start benchmarks on battery power too")
    watch.add_argument("--poll", type=float, default=300, metavar="SECONDS", help="how often to fetch both repositories")
    watch.add_argument("--once", action="store_true", help="print what would be benchmarked next and exit")

    window = subparsers.add_parser("window", help="list, add or remove the local-time windows benchmarks may start in")
    window.add_argument("action", nargs="?", choices=["list", "add", "remove", "clear"], default="list")
    window.add_argument("spec", nargs="?", help="a window such as 22:00-06:00, 22-6 or 10pm-6am")

    upload_parser = subparsers.add_parser("upload", help="upload results the Worker has not acknowledged")
    upload_parser.add_argument("--dry-run", action="store_true")
    upload_parser.add_argument("--limit", type=int, default=0)

    forget = subparsers.add_parser("forget", help="make a terminal commit eligible for retry")
    forget.add_argument("commit")
    forget.add_argument("--aihc-repo", help=argparse.SUPPRESS)

    return parser
