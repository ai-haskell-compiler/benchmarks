from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .catalog import build_bundle, write_catalog
from .config import ConfigError, detect_platform, experiment_id, load_config
from .database import Database
from .git_history import GitError, commits, fetch
from .planner import select_next
from .publisher import PublishError, publish
from .report import generate_summary, load_json, update_readme
from .runner import run_commit


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
        database = Database(state_path)
        try:
            _dispatch(arguments, root, config, platform_id, experiment, database)
        finally:
            database.close()
    except (ConfigError, GitError, PublishError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")


def _dispatch(
    arguments: argparse.Namespace,
    root: Path,
    config: Dict[str, Any],
    platform_id: str,
    experiment: str,
    database: Database,
) -> None:
    if arguments.command == "doctor":
        _doctor(arguments, root, config, platform_id, experiment)
        return
    if arguments.command == "report":
        catalog = load_json(arguments.catalog)
        summary = generate_summary(catalog)
        update_readme((root / arguments.readme).resolve(), summary)
        write_catalog(catalog, (root / arguments.site_catalog).resolve())
        print(f"updated {arguments.readme} and {arguments.site_catalog}")
        return

    if arguments.command in {"plan", "run"}:
        repository = _repository(arguments)
        if arguments.fetch:
            fetch(repository)
        history = commits(repository, config.get("aihc_ref", "origin/main"))
        database.replace_commits(history)

    if arguments.command == "plan":
        terminal = database.terminal_attempts(experiment, platform_id)
        next_commit = select_next(database.commits(), terminal)
        print(f"experiment: {experiment}")
        print(f"platform:   {platform_id}")
        print(f"complete:   {len(terminal)}/{len(history)}")
        if next_commit:
            print(f"next:       {next_commit['sha']}  {next_commit['subject']}")
        else:
            print("next:       none")
        return

    if arguments.command == "run":
        repository = _repository(arguments)
        completed = 0
        while True:
            terminal = database.terminal_attempts(experiment, platform_id)
            next_commit = select_next(database.commits(), terminal)
            if not next_commit:
                print("all commits have terminal results")
                break
            print(f"benchmarking {next_commit['sha'][:12]} ({next_commit['ordinal'] + 1}/{len(history)}): {next_commit['subject']}")
            envelope = run_commit(
                database=database,
                config=config,
                experiment_id=experiment,
                platform_id=platform_id,
                commit=next_commit,
                aihc_repository=repository,
                root=root,
                jobs=arguments.jobs,
            )
            print(f"recorded {envelope['compiler_status']} result {envelope['run_id']}")
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

    if arguments.command == "publish":
        envelopes = database.result_envelopes(experiment, platform_id)
        if not envelopes:
            raise ValueError("there are no terminal results to publish")
        publishing = config["publishing"]
        public_base_url = os.environ.get("R2_PUBLIC_BASE_URL", publishing["public_base_url"])
        default_base = f"{public_base_url.rstrip('/')}/{publishing['candidate_catalog_key'].lstrip('/')}"
        catalog = publish(
            envelopes=envelopes,
            config=config,
            destination=(root / arguments.output).resolve(),
            base_catalog_location=arguments.base_catalog or default_base,
            dry_run=arguments.dry_run,
            trigger_workflow=arguments.trigger_workflow,
            root=root,
        )
        print(f"prepared catalog with {len(catalog.get('series', []))} result views")
        return
    raise ValueError(f"unsupported command {arguments.command}")


def _doctor(arguments: argparse.Namespace, root: Path, config: Dict[str, Any], platform_id: str, experiment: str) -> None:
    repository = _repository(arguments)
    failures = []
    print(f"experiment: {experiment}")
    print(f"platform:   {platform_id}")
    print(f"aihc repo:  {repository}")
    for executable in ("git", "nix"):
        resolved = shutil.which(executable)
        print(f"{executable:10} {resolved or 'missing'}")
        if not resolved:
            failures.append(executable)
    if not (repository / ".git").exists() and not (repository / "HEAD").exists():
        failures.append("aihc repository")
        print("repository does not appear to be a Git checkout")
    publishing = config.get("publishing", {})
    print(f"public URL: {os.environ.get('R2_PUBLIC_BASE_URL', publishing.get('public_base_url', 'not configured'))}")
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

    doctor = subparsers.add_parser("doctor", help="validate the local environment")
    doctor.add_argument("--aihc-repo")

    plan = subparsers.add_parser("plan", help="show coverage and the next maximally spaced commit")
    plan.add_argument("--aihc-repo")
    plan.add_argument("--fetch", action="store_true")

    run = subparsers.add_parser("run", help="benchmark the next commit")
    run.add_argument("--aihc-repo")
    run.add_argument("--fetch", action="store_true")
    run.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    run.add_argument("--all", action="store_true", help="continue until all commits are terminal")
    run.add_argument("--limit", type=int, default=0, help="maximum commits for --all; zero means unlimited")

    forget = subparsers.add_parser("forget", help="make a terminal commit eligible for retry")
    forget.add_argument("commit")

    publish_parser = subparsers.add_parser("publish", help="build and optionally upload an R2 result catalog")
    publish_parser.add_argument("--output", default="result-bundles")
    publish_parser.add_argument("--base-catalog")
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.add_argument("--trigger-workflow", action="store_true")

    report = subparsers.add_parser("report", help="regenerate the README and checked-in site catalog")
    report.add_argument("--catalog", required=True)
    report.add_argument("--readme", default="README.md")
    report.add_argument("--site-catalog", default="site/data/catalog.json")
    return parser
