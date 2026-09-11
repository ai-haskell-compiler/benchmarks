from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .compare import CompareError, format_report, resolve_side, run_compare, select_configuration, worktree_side
from .config import ConfigError, detect_platform, experiment_id, load_config
from .database import Database
from .git_history import GitError, commits, fetch
from .machine import load_machine
from .planner import build_plan
from .runner import run_commit
from .uploader import UploadError, check_login, upload_pending


def main(argv: Optional[list] = None) -> None:
    parser = _parser()
    arguments = parser.parse_args(argv)
    root = Path(arguments.root).resolve()
    try:
        config = load_config((root / arguments.config).resolve())
        platform_id = arguments.platform or detect_platform()
        if platform_id not in config["platforms"]:
            raise ConfigError(f"platform {platform_id!r} is not configured")
        experiment = experiment_id(config)
        state_path = (root / arguments.state).resolve()
        machine = load_machine(state_path.parent, getattr(arguments, "machine", None))
        database = Database(state_path)
        try:
            _dispatch(arguments, root, config, platform_id, experiment, database, machine)
        finally:
            database.close()
    except (CompareError, ConfigError, GitError, UploadError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")


def _dispatch(
    arguments: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    platform_id: str,
    experiment: str,
    database: Database,
    machine: Dict[str, Any],
) -> None:
    if arguments.command == "doctor":
        _doctor(arguments, root, config, platform_id, experiment, machine)
        return
    if arguments.command == "compare":
        repository = _repository(arguments)
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
            jobs=arguments.jobs,
            log=lambda message: print(message, file=sys.stderr),
        )
        database.record_adhoc(report)
        print(format_report(report, markdown=arguments.markdown))
        return

    if arguments.command == "upload":
        if not arguments.dry_run:
            check_login(config, root)
        summary = upload_pending(
            database, experiment, platform_id, config, root, database.commits(), dry_run=arguments.dry_run, limit=arguments.limit
        )
        print(f"uploaded {summary['uploaded']} runs ({summary['pending']} still pending)")
        return

    if arguments.command in {"plan", "run"}:
        repository = _repository(arguments)
        if arguments.fetch:
            fetch(repository)
        history = commits(
            repository, config.get("aihc_ref", "origin/main"), config["aihc_tree_paths"], since=config.get("aihc_since")
        )
        database.replace_commits(history)
        database.propagate_inherited(experiment, platform_id)

    if arguments.command == "plan":
        _print_plan(database, experiment, platform_id, len(history))
        return

    if arguments.command == "run":
        repository = _repository(arguments)
        if arguments.upload:
            check_login(config, root)
        completed = 0
        while True:
            terminal = database.terminal_attempts(experiment, platform_id)
            plan = build_plan(database.commits(), terminal)
            next_commit = plan["next"]
            if not next_commit:
                print("all commits have terminal results")
                break
            print(f"benchmarking {next_commit['sha'][:12]} ({next_commit['ordinal'] + 1}/{len(history)}, {plan['stage']}): {next_commit['subject']}")
            envelope = run_commit(
                database=database,
                config=config,
                experiment_id=experiment,
                platform_id=platform_id,
                machine=machine,
                commit=next_commit,
                aihc_repository=repository,
                root=root,
                jobs=arguments.jobs,
            )
            print(f"recorded {envelope['compiler_status']} result {envelope['run_id']}")
            inherited = database.propagate_inherited(experiment, platform_id)
            if inherited:
                print(f"propagated the result to {inherited} same-tree commits")
            if arguments.upload:
                summary = upload_pending(database, experiment, platform_id, config, root, database.commits())
                print(f"uploaded {summary['uploaded']} runs ({summary['pending']} still pending)")
            completed += 1
            if not arguments.all or (arguments.limit and completed >= arguments.limit):
                break
        return

    if arguments.command == "forget":
        sha = _resolve_commit(database, arguments.commit)
        if database.forget(experiment, platform_id, sha):
            print(f"forgot active result for {sha}")
        else:
            raise ValueError(f"no active result for {sha}")
        return

    raise ValueError(f"unsupported command {arguments.command}")


def _print_plan(database: Database, experiment: str, platform_id: str, total: int) -> None:
    terminal = database.terminal_attempts(experiment, platform_id)
    plan = build_plan(database.commits(), terminal)
    measured = sum(1 for attempt in terminal if not attempt.get("inherited_from"))
    inherited = len(terminal) - measured
    print(f"experiment: {experiment}")
    print(f"platform:   {platform_id}")
    print(f"complete:   {len(terminal)}/{total} ({measured} measured, {inherited} inherited)")
    next_commit = plan["next"]
    if next_commit:
        print(f"next:       {next_commit['sha']}  {next_commit['subject']}  [{plan['stage']}]")
    else:
        print("next:       none")
    if plan["gaps"]:
        print("gaps:       ordinals      width  signal  recency  score")
        for gap in plan["gaps"][:5]:
            span = f"{gap['start']['ordinal'] + 1}-{gap['end']['ordinal'] + 1}"
            print(f"            {span:12} {gap['width']:5}  {gap['signal']:.3f}  {gap['recency']:.3f}  {gap['score']:8.1f}")


def _doctor(
    arguments: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    platform_id: str,
    experiment: str,
    machine: Dict[str, Any],
) -> None:
    repository = _repository(arguments)
    failures = []
    derivation = machine.get("derivation", {})
    print(f"experiment: {experiment}")
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


def _repository(arguments: argparse.Namespace) -> Path:
    value = arguments.aihc_repo or os.environ.get("AIHC_REPOSITORY")
    if not value:
        raise ValueError("provide --aihc-repo or set AIHC_REPOSITORY")
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
    doctor.add_argument("--aihc-repo")
    doctor.add_argument("--machine", help="override and freeze the derived machine id")

    plan = subparsers.add_parser("plan", help="show coverage, the next commit, and the highest-scoring gaps")
    plan.add_argument("--aihc-repo")
    plan.add_argument("--fetch", action="store_true")

    run = subparsers.add_parser("run", help="benchmark the next commit")
    run.add_argument("--aihc-repo")
    run.add_argument("--fetch", action="store_true")
    run.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    run.add_argument("--all", action="store_true", help="continue until all commits are terminal")
    run.add_argument("--limit", type=int, default=0, help="maximum commits for --all; zero means unlimited")
    run.add_argument("--upload", action="store_true", help="upload results to the Worker after each commit")

    compare = subparsers.add_parser("compare", help="benchmark two AIHC builds against each other, locally")
    compare.add_argument("a", help="commit, branch or tag for side A")
    compare.add_argument("b", nargs="?", help="commit, branch or tag for side B")
    compare.add_argument("--worktree", help="use this AIHC checkout, including uncommitted changes, as side B")
    compare.add_argument("--aihc-repo")
    compare.add_argument("--bench", action="append", default=[], metavar="ID", help="benchmark id; repeatable, default all")
    compare.add_argument("--config", dest="config_ids", action="append", default=[], metavar="ID", help="configuration id; repeatable, default every AIHC configuration")
    compare.add_argument("--profile", choices=["O0", "O2"])
    compare.add_argument("--rounds", type=int, default=10, help="interleaved A/B rounds per cell")
    compare.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    compare.add_argument("--markdown", action="store_true", help="print a Markdown table")

    upload_parser = subparsers.add_parser("upload", help="upload results the Worker has not acknowledged")
    upload_parser.add_argument("--dry-run", action="store_true")
    upload_parser.add_argument("--limit", type=int, default=0)

    forget = subparsers.add_parser("forget", help="make a terminal commit eligible for retry")
    forget.add_argument("commit")
    forget.add_argument("--aihc-repo", help=argparse.SUPPRESS)

    return parser
