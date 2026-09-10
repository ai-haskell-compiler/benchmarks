from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import ConfigError, detect_platform, experiment_id, load_config
from .database import Database
from .git_history import GitError, commits, fetch
from .machine import load_machine
from .planner import build_plan
from .runner import run_commit
from .uploader import UploadError, load_credentials, register, save_credentials, upload_pending


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
    except (ConfigError, GitError, UploadError, ValueError) as error:
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
    if arguments.command == "register":
        admin_token = os.environ.get("AIHC_BENCH_ADMIN_TOKEN")
        if not admin_token:
            raise ValueError("set AIHC_BENCH_ADMIN_TOKEN to the Worker's admin token")
        server = arguments.server or config["publishing"]["server_url"]
        token = register(server, admin_token, machine["machine_id"], arguments.display_name)
        path = save_credentials((root / arguments.state).resolve().parent, server, token)
        print(f"registered {machine['machine_id']} with {server}; token stored in {path}")
        return

    if arguments.command == "upload":
        credentials = load_credentials((root / arguments.state).resolve().parent)
        if not credentials:
            raise ValueError("no upload credentials; run `aihc-bench register` first")
        summary = upload_pending(
            database, experiment, platform_id, credentials, database.commits(), dry_run=arguments.dry_run, limit=arguments.limit
        )
        print(f"uploaded {summary['uploaded']} runs ({summary['skipped']} already known, {summary['pending']} still pending)")
        return

    if arguments.command in {"plan", "run"}:
        repository = _repository(arguments)
        if arguments.fetch:
            fetch(repository)
        history = commits(repository, config.get("aihc_ref", "origin/main"), config["aihc_tree_paths"])
        database.replace_commits(history)
        database.propagate_inherited(experiment, platform_id)

    if arguments.command == "plan":
        _print_plan(database, experiment, platform_id, len(history))
        return

    if arguments.command == "run":
        repository = _repository(arguments)
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
                credentials = load_credentials((root / arguments.state).resolve().parent)
                if not credentials:
                    raise ValueError("no upload credentials; run `aihc-bench register` first")
                summary = upload_pending(database, experiment, platform_id, credentials, database.commits())
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
    for executable in ("git", "nix"):
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

    register_parser = subparsers.add_parser("register", help="register this machine with the Worker and store its upload token")
    register_parser.add_argument("--server", help="Worker URL; defaults to publishing.server_url")
    register_parser.add_argument("--display-name")

    upload_parser = subparsers.add_parser("upload", help="upload results the Worker has not acknowledged")
    upload_parser.add_argument("--dry-run", action="store_true")
    upload_parser.add_argument("--limit", type=int, default=0)

    forget = subparsers.add_parser("forget", help="make a terminal commit eligible for retry")
    forget.add_argument("commit")
    forget.add_argument("--aihc-repo", help=argparse.SUPPRESS)

    return parser
