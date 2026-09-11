"""Upload local results to Cloudflare through wrangler.

Authorization is whatever ``wrangler login`` granted on this machine: envelopes
go to the R2 bucket with ``wrangler r2 object put`` and index rows go to the D1
database with ``wrangler d1 execute``. The Worker itself is read-only. Uploads
are idempotent: rows are inserted with ``INSERT OR IGNORE`` on their primary
keys, and the local database records ``uploaded_at`` only after both commands
succeeded, so an interrupted upload resumes where it stopped.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import SCHEMA_VERSION, __version__
from .database import Database
from .schema import utc_now

COMMIT_CHUNK = 500
LOGGED_IN_MARKER = "logged in"
STATEMENT_CHUNK = 400

Run = Callable[[List[str]], subprocess.CompletedProcess]


class UploadError(RuntimeError):
    pass


def wrangler_command(config: Dict[str, Any], root: Path, *arguments: str) -> List[str]:
    """A wrangler invocation bound to the Worker's configuration file."""
    return ["wrangler", "--config", str(root / config["publishing"]["wrangler_config"]), *arguments]


def check_login(config: Dict[str, Any], root: Path, run: Run = None) -> str:  # type: ignore[assignment]
    """Return the account line from ``wrangler whoami`` or raise."""
    run = run or _run
    process = run(wrangler_command(config, root, "whoami"))
    output = f"{process.stdout}\n{process.stderr}"
    if process.returncode != 0 or "not authenticated" in output.lower() or LOGGED_IN_MARKER not in output.lower():
        raise UploadError("wrangler is not authenticated on this machine; run `wrangler login` first")
    for line in output.splitlines():
        if LOGGED_IN_MARKER in line.lower():
            return line.strip()
    return LOGGED_IN_MARKER


def upload_pending(
    database: Database,
    experiments: Dict[str, str],
    suite: str,
    platform_id: str,
    config: Dict[str, Any],
    root: Path,
    commits: Iterable[Dict[str, Any]],
    *,
    dry_run: bool = False,
    limit: int = 0,
    run: Run = None,  # type: ignore[assignment]
    log: Callable[[str], None] = print,
) -> Dict[str, int]:
    """Push the commit list, every run the remote has not acknowledged, then the suite.

    ``experiments`` maps benchmark id to experiment id. The suite record is
    written last so the Worker only switches to a suite whose runs it has.
    """
    run = run or _run
    pending = [
        (experiment_id, attempt)
        for experiment_id in experiments.values()
        for attempt in database.pending_uploads(experiment_id, platform_id)
    ]
    if limit:
        pending = pending[:limit]
    summary = {"commits": 0, "uploaded": 0, "pending": len(pending)}
    if dry_run:
        for _, attempt in pending:
            log(f"would upload {attempt['commit_sha'][:12]} {attempt['experiment_id']} ({attempt['status']})")
        return summary

    commit_list = list(commits)
    for start in range(0, len(commit_list), COMMIT_CHUNK):
        chunk = commit_list[start : start + COMMIT_CHUNK]
        _execute_sql(config, root, [commit_statement(commit) for commit in chunk], run)
        summary["commits"] += len(chunk)

    bucket = config["publishing"]["bucket"]
    for experiment_id, attempt in pending:
        envelope = json.loads(attempt["result_json"])
        if envelope.get("schema_version") != SCHEMA_VERSION:
            raise UploadError(f"{attempt['commit_sha'][:12]} has schema version {envelope.get('schema_version')}, expected {SCHEMA_VERSION}")
        key = envelope_key(envelope)
        if not envelope.get("inherited_from"):
            _put_object(bucket, key, envelope, run)
        _execute_sql(config, root, run_statements(envelope, key), run)
        database.mark_uploaded(experiment_id, platform_id, attempt["commit_sha"], utc_now())
        summary["uploaded"] += 1
        log(f"uploaded {attempt['commit_sha'][:12]} {envelope.get('benchmark') or experiment_id}{' (inherited)' if envelope.get('inherited_from') else ''}")
    _execute_sql(config, root, [suite_statement(suite, config["suite_id"], experiments)], run)
    summary["pending"] = sum(len(database.pending_uploads(experiment_id, platform_id)) for experiment_id in experiments.values())
    return summary


def refresh_overview(
    config: Dict[str, Any],
    *,
    opener: Callable[[str, float], Any] = None,  # type: ignore[assignment]
    log: Callable[[str], None] = print,
) -> bool:
    """Ask the Worker to recompute its materialized overview after an upload.

    The front page is served from that copy, so without this the first visitor
    after an upload would still see the previous results. Failure is logged
    and never fails the upload.
    """
    opener = opener or _open_url
    try:
        url = overview_refresh_url(config["publishing"]["server_url"])
        opener(url, 60.0)
    except (OSError, ValueError) as error:
        log(f"warning: could not refresh the overview at {config['publishing']['server_url']}: {error}")
        return False
    return True


def overview_refresh_url(server_url: str) -> str:
    """The refresh endpoint on the configured server, which must be an https origin."""
    parts = urllib.parse.urlsplit(server_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"server_url must be an https URL, got {server_url!r}")
    host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return urllib.parse.urlunsplit(("https", host, "/api/overview", "refresh=1", ""))


# Cloudflare's bot protection rejects urllib's default ``Python-urllib/x.y``
# agent with a 403 before the Worker ever sees the request, so the refresh
# identifies itself as the runner instead.
USER_AGENT = f"aihc-bench/{__version__}"


def _open_url(url: str, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


# ---------------------------------------------------------------------------
# SQL generation


def envelope_key(envelope: Dict[str, Any]) -> str:
    """R2 key of the envelope. Inherited runs point at their source's envelope."""
    sha = envelope.get("inherited_from") or envelope["aihc_commit"]["sha"]
    return f"raw/v2/{envelope['machine_id']}/{sha}/{envelope['run_id']}.json.gz"


def run_row_id(envelope: Dict[str, Any]) -> str:
    if envelope.get("inherited_from"):
        return f"{envelope['run_id']}~{envelope['aihc_commit']['sha'][:12]}"
    return envelope["run_id"]


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def commit_statement(commit: Dict[str, Any]) -> str:
    return (
        "INSERT INTO commits(sha, ordinal, committed_at, subject, tree_key) VALUES ("
        + ", ".join(sql_literal(commit.get(key)) for key in ("sha", "ordinal", "committed_at", "subject", "tree_key"))
        + ") ON CONFLICT(sha) DO UPDATE SET ordinal = excluded.ordinal, committed_at = excluded.committed_at, "
        "subject = excluded.subject, tree_key = COALESCE(excluded.tree_key, commits.tree_key);"
    )


def suite_statement(suite: str, suite_id: str, experiments: Dict[str, str]) -> str:
    """Record the suite and bump it to the most recently uploaded one.

    The Worker treats the suite with the newest ``uploaded_at`` as active and
    reads its benchmark to experiment mapping from ``experiments``.
    """
    mapping = json.dumps(dict(sorted(experiments.items())), sort_keys=True, separators=(",", ":"))
    return (
        "INSERT INTO suites(suite_key, suite_id, experiments, uploaded_at) VALUES ("
        + ", ".join(sql_literal(value) for value in (suite, suite_id, mapping, utc_now()))
        + ") ON CONFLICT(suite_key) DO UPDATE SET experiments = excluded.experiments, uploaded_at = excluded.uploaded_at;"
    )


def run_statements(envelope: Dict[str, Any], key: str) -> List[str]:
    now = utc_now()
    machine = envelope["machine_id"]
    commit = envelope["aihc_commit"]
    environment = envelope["environment"]
    row_id = run_row_id(envelope)
    statements = [
        # token_hash is a legacy column that D1 cannot drop; it stays empty.
        f"INSERT INTO machines(machine_id, token_hash, created_at, last_seen_at) VALUES ({sql_literal(machine)}, '', {sql_literal(now)}, {sql_literal(now)}) "
        f"ON CONFLICT(machine_id) DO UPDATE SET last_seen_at = excluded.last_seen_at;",
        commit_statement(commit),
        f"INSERT OR IGNORE INTO environments(environment_id, machine_id, first_seen_at, record) VALUES ("
        f"{sql_literal(environment['id'])}, {sql_literal(machine)}, {sql_literal(now)}, {sql_literal(json.dumps(environment, sort_keys=True))});",
        "INSERT OR IGNORE INTO runs(run_id, machine_id, environment_id, experiment_id, commit_sha, commit_ordinal, compiler_status, "
        "unavailable_reason, inherited_from, created_at, uploaded_at, envelope_key) VALUES ("
        + ", ".join(
            sql_literal(value)
            for value in (
                row_id,
                machine,
                environment["id"],
                envelope["experiment_id"],
                commit["sha"],
                commit["ordinal"],
                envelope["compiler_status"],
                envelope.get("unavailable_reason"),
                envelope.get("inherited_from"),
                envelope["created_at"],
                now,
                key,
            )
        )
        + ");",
    ]
    for result in envelope.get("results", []):
        measurement = result.get("measurement", {})
        for metric in measurement.get("metrics", []):
            estimate = metric.get("estimate")
            if measurement.get("status") in ("converged", "nonconverged"):
                status = "unavailable" if estimate is None else metric.get("status", "ok")
            else:
                status = measurement.get("status", "unavailable")
            samples = metric.get("samples")
            statements.append(
                "INSERT OR REPLACE INTO measurements(run_id, machine_id, experiment_id, commit_ordinal, benchmark, configuration, "
                "compiler_family, compiler_version, backend, optimization, baseline, metric, unit, status, estimate, sample_count) VALUES ("
                + ", ".join(
                    sql_literal(value)
                    for value in (
                        row_id,
                        machine,
                        envelope["experiment_id"],
                        commit["ordinal"],
                        result["benchmark"],
                        result["configuration"],
                        result["compiler_family"],
                        result.get("compiler_version", ""),
                        result["backend"],
                        result.get("optimization", "O2"),
                        bool(result.get("baseline")),
                        metric["metric"],
                        metric["unit"],
                        status,
                        estimate,
                        len(samples) if isinstance(samples, list) else None,
                    )
                )
                + ");"
            )
    return statements


# ---------------------------------------------------------------------------
# wrangler plumbing


def _run(command: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


def _execute_sql(config: Dict[str, Any], root: Path, statements: List[str], run: Run) -> None:
    database = config["publishing"]["database"]
    for start in range(0, len(statements), STATEMENT_CHUNK):
        chunk = statements[start : start + STATEMENT_CHUNK]
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as handle:
            handle.write("\n".join(chunk) + "\n")
            path = handle.name
        try:
            process = run(wrangler_command(config, root, "d1", "execute", database, "--remote", "--file", path, "--json"))
        finally:
            Path(path).unlink(missing_ok=True)
        if process.returncode != 0:
            raise UploadError(f"wrangler d1 execute failed:\n{(process.stderr or process.stdout)[-2000:]}")


def _put_object(bucket: str, key: str, envelope: Dict[str, Any], run: Run) -> None:
    payload = gzip.compress(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    with tempfile.NamedTemporaryFile("wb", suffix=".json.gz", delete=False) as handle:
        handle.write(payload)
        path = handle.name
    try:
        process = run(
            [
                "wrangler",
                "r2",
                "object",
                "put",
                f"{bucket}/{key}",
                "--file",
                path,
                "--content-type",
                "application/json",
                "--content-encoding",
                "gzip",
                "--cache-control",
                "public, max-age=31536000, immutable",
                "--remote",
            ]
        )
    finally:
        Path(path).unlink(missing_ok=True)
    if process.returncode != 0:
        raise UploadError(f"wrangler r2 object put failed for {key}:\n{(process.stderr or process.stdout)[-2000:]}")
