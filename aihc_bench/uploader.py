"""Upload local results to the fast.aihc.app Worker.

Credentials live in ``.state/upload.json`` as ``{"server": ..., "token": ...}``.
The token is issued once per machine by ``register`` and never printed again.
Uploads are idempotent: the Worker keys runs by ``run_id`` and the local
database records ``uploaded_at`` only after the Worker acknowledged the run.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import __version__
from .database import Database
from .schema import utc_now

CREDENTIALS_FILE = "upload.json"
COMMIT_CHUNK = 500

HttpPost = Callable[[str, bytes, Dict[str, str]], Tuple[int, Dict[str, Any]]]


class UploadError(RuntimeError):
    pass


def load_credentials(state_dir: Path) -> Optional[Dict[str, str]]:
    path = state_dir / CREDENTIALS_FILE
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise UploadError(f"could not read {path}: {error}") from error
    if not record.get("server") or not record.get("token"):
        raise UploadError(f"{path} lacks server or token")
    return {"server": str(record["server"]).rstrip("/"), "token": str(record["token"])}


def save_credentials(state_dir: Path, server: str, token: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / CREDENTIALS_FILE
    path.write_text(json.dumps({"server": server.rstrip("/"), "token": token}, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def register(
    server: str,
    admin_token: str,
    machine_id: str,
    display_name: Optional[str],
    post: HttpPost = None,  # type: ignore[assignment]
) -> str:
    """Register this machine with the Worker and return its upload token."""
    post = post or http_post
    body = json.dumps({"machine_id": machine_id, "display_name": display_name}).encode("utf-8")
    status, payload = post(f"{server.rstrip('/')}/api/machines", body, _headers(admin_token))
    if status != 201 or not payload.get("token"):
        raise UploadError(f"registration failed with status {status}: {payload.get('error', payload)}")
    return str(payload["token"])


def upload_pending(
    database: Database,
    experiment_id: str,
    platform_id: str,
    credentials: Dict[str, str],
    commits: Iterable[Dict[str, Any]],
    *,
    dry_run: bool = False,
    limit: int = 0,
    post: HttpPost = None,  # type: ignore[assignment]
    log: Callable[[str], None] = print,
) -> Dict[str, int]:
    """Push the first-parent commit list, then every run not yet acknowledged."""
    post = post or http_post
    server = credentials["server"]
    headers = _headers(credentials["token"])
    pending = database.pending_uploads(experiment_id, platform_id)
    if limit:
        pending = pending[:limit]
    summary = {"commits": 0, "uploaded": 0, "skipped": 0, "pending": len(pending)}
    if dry_run:
        for attempt in pending:
            log(f"would upload {attempt['commit_sha'][:12]} ({attempt['status']})")
        return summary

    commit_list = [
        {key: commit[key] for key in ("sha", "ordinal", "committed_at", "subject", "tree_key") if key in commit}
        for commit in commits
    ]
    for start in range(0, len(commit_list), COMMIT_CHUNK):
        chunk = commit_list[start : start + COMMIT_CHUNK]
        body = gzip.compress(json.dumps({"commits": chunk}).encode("utf-8"))
        status, payload = post(f"{server}/api/commits", body, {**headers, "Content-Encoding": "gzip"})
        if status != 200:
            raise UploadError(f"commit upload failed with status {status}: {payload.get('error', payload)}")
        summary["commits"] += len(chunk)

    for attempt in pending:
        envelope = json.loads(attempt["result_json"])
        body = gzip.compress(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        status, payload = post(f"{server}/api/upload", body, {**headers, "Content-Encoding": "gzip"})
        if status not in (200, 201):
            raise UploadError(
                f"upload of {attempt['commit_sha'][:12]} failed with status {status}: {payload.get('error', payload)}"
            )
        database.mark_uploaded(experiment_id, platform_id, attempt["commit_sha"], utc_now())
        if payload.get("inserted"):
            summary["uploaded"] += 1
            log(f"uploaded {attempt['commit_sha'][:12]} as {payload.get('run_id')}")
        else:
            summary["skipped"] += 1
    summary["pending"] = len(database.pending_uploads(experiment_id, platform_id))
    return summary


def _headers(token: str) -> Dict[str, str]:
    # Cloudflare's bot protection rejects the default urllib user agent.
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": f"aihc-bench/{__version__}",
    }


def http_post(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decode(error.read())
    except urllib.error.URLError as error:
        raise UploadError(f"could not reach {url}: {error.reason}") from error


def _decode(payload: bytes) -> Dict[str, Any]:
    try:
        decoded = json.loads(payload or b"{}")
    except ValueError:
        return {"error": payload[:200].decode("utf-8", errors="replace")}
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def pending_count(database: Database, experiment_id: str, platform_id: str) -> int:
    return len(database.pending_uploads(experiment_id, platform_id))


def list_pending(database: Database, experiment_id: str, platform_id: str) -> List[Dict[str, Any]]:
    return database.pending_uploads(experiment_id, platform_id)
