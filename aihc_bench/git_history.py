from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List


class GitError(RuntimeError):
    pass


def fetch(repository: Path) -> None:
    _git(repository, "fetch", "--prune", "origin", "main")


def commits(repository: Path, ref: str) -> List[Dict[str, Any]]:
    output = _git(repository, "log", "--first-parent", "--reverse", "--format=%H%x09%cI%x09%s", ref)
    history: List[Dict[str, Any]] = []
    for ordinal, line in enumerate(output.splitlines()):
        sha, committed_at, subject = line.split("\t", 2)
        history.append({"sha": sha, "ordinal": ordinal, "committed_at": committed_at, "subject": subject})
    if not history:
        raise GitError(f"no commits found at {ref}")
    return history


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
