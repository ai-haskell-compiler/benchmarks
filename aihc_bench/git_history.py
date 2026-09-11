from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Paths whose content determines the compiler that a commit builds. Commits
# that leave all of them untouched produce the same compiler as their parent
# and inherit its results.
DEFAULT_TREE_PATHS = (
    "bin/aihc",
    "components",
    "core-libs",
    "tooling",
    "cabal.project",
    "flake.nix",
    "flake.lock",
    "scripts/nix",
)


class GitError(RuntimeError):
    pass


def fetch(repository: Path) -> None:
    _git(repository, "fetch", "--prune", "origin", "main")


def commits(
    repository: Path,
    ref: str,
    tree_paths: Iterable[str] = DEFAULT_TREE_PATHS,
    since: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List the first-parent history of ``ref``, oldest first.

    ``since`` is an ISO 8601 timestamp (a date alone means midnight UTC);
    commits committed before it are left out because they predate a usable
    compiler. Ordinals are positions in the full first-parent history, so
    moving the cutoff never renumbers the commits that remain.
    """
    cutoff = parse_cutoff(since) if since else None
    output = _git(repository, "log", "--first-parent", "--reverse", "--format=%H%x09%ct%x09%cI%x09%s", ref)
    history: List[Dict[str, Any]] = []
    for ordinal, line in enumerate(output.splitlines()):
        sha, committed_seconds, committed_at, subject = line.split("\t", 3)
        if cutoff is not None and int(committed_seconds) < cutoff:
            continue
        history.append({"sha": sha, "ordinal": ordinal, "committed_at": committed_at, "subject": subject})
    if not history:
        raise GitError(f"no commits found at {ref}" + (f" since {since}" if since else ""))
    keys = tree_keys(repository, [commit["sha"] for commit in history], tree_paths)
    for commit in history:
        commit["tree_key"] = keys[commit["sha"]]
    return history


def parse_cutoff(timestamp: str) -> int:
    """Turn an ISO 8601 timestamp into Unix seconds, reading a missing zone as UTC."""
    text = timestamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise GitError(f"invalid timestamp {timestamp!r}: expected ISO 8601 such as 2026-09-01") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def tree_keys(repository: Path, shas: List[str], tree_paths: Iterable[str]) -> Dict[str, str]:
    """Hash the object IDs of ``tree_paths`` for every commit in one Git call.

    A path missing from a commit hashes as ``missing``, so adding or removing a
    tracked directory changes the key like any other edit.
    """
    paths = list(tree_paths)
    if not paths:
        return {sha: "no-paths" for sha in shas}
    queries = "".join(f"{sha}:{path}\n" for sha in shas for path in paths)
    process = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch-check=%(objectname)"],
        input=queries,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise GitError(process.stderr.strip() or "git cat-file failed")
    lines = process.stdout.splitlines()
    expected = len(shas) * len(paths)
    if len(lines) != expected:
        raise GitError(f"git cat-file returned {len(lines)} lines for {expected} queries")
    keys: Dict[str, str] = {}
    index = 0
    for sha in shas:
        hasher = hashlib.sha256()
        for path in paths:
            line = lines[index]
            index += 1
            object_id = "missing" if line.endswith(" missing") else line.split()[0]
            hasher.update(f"{path}={object_id}\n".encode("utf-8"))
        keys[sha] = hasher.hexdigest()[:16]
    return keys


def path_exists(repository: Path, sha: str, path: str) -> bool:
    process = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{sha}:{path}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return process.returncode == 0


def create_worktree(repository: Path, destination: Path, sha: str) -> None:
    if destination.exists():
        remove_worktree(repository, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", "--force", str(destination), sha)


def remove_worktree(repository: Path, destination: Path) -> None:
    if destination.exists():
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "remove", "--force", str(destination)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    if destination.exists():
        shutil.rmtree(destination)
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _git(repository: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository), *arguments], stderr=subprocess.STDOUT, text=True
        ).strip()
    except subprocess.CalledProcessError as error:
        raise GitError(error.output.strip()) from error
