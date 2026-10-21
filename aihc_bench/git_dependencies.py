"""Pinned dependency sources, fetched and staged before compilation is timed."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .process import run_command


class GitDependencyError(ValueError):
    pass


def validate_git_dependencies(dependencies: Any) -> None:
    if not isinstance(dependencies, list):
        raise GitDependencyError("git_dependencies must be a list")
    names = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"url", "commit", "packages"}:
            raise GitDependencyError("each Git dependency needs url, commit and packages")
        url = dependency["url"]
        if not isinstance(url, str) or not url.strip() or url.startswith("-"):
            raise GitDependencyError("Git dependency url must be non-empty")
        commit = dependency["commit"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise GitDependencyError("Git dependency commit must be a full 40-character lowercase SHA")
        packages = dependency["packages"]
        if not isinstance(packages, dict) or not packages:
            raise GitDependencyError("Git dependency packages must map package names to repository subdirectories")
        for name, subdir in packages.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", name)
                    or not any(c.isalpha() for c in name)):
                raise GitDependencyError(f"invalid Git dependency package name: {name}")
            if name in names:
                raise GitDependencyError(f"duplicate Git dependency package: {name}")
            names.add(name)
            if (not isinstance(subdir, str) or not subdir or "\\" in subdir
                    or PurePosixPath(subdir).is_absolute() or ".." in PurePosixPath(subdir).parts):
                raise GitDependencyError(f"Git dependency {name}: subdirectory must stay inside the repository")


def package_names(dependencies: list[dict]) -> list[str]:
    return sorted(name for dependency in dependencies for name in dependency["packages"])


def _git(arguments: list[str], cwd: Path, timeout: float) -> str:
    try:
        result = run_command(["git", *arguments], cwd, timeout, {"GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitDependencyError(f"Git dependency preparation failed: {error}") from error
    if result.returncode:
        raise GitDependencyError(f"Git dependency preparation failed:\n{(result.stderr or result.stdout)[-8192:]}")
    return result.stdout.strip()


def _checkout(dependency: dict, cache: Path, timeout: float) -> Path:
    """Publish a complete checkout atomically; never build in this cache."""
    key = hashlib.sha256((dependency["url"] + "\n" + dependency["commit"]).encode()).hexdigest()
    destination = cache / key
    if destination.is_dir():
        return destination
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as temporary:
        checkout = Path(temporary) / "source"
        checkout.mkdir()
        _git(["init", "--quiet"], checkout, timeout)
        _git(["fetch", "--quiet", "--depth=1", "--no-tags", dependency["url"], dependency["commit"]], checkout, timeout)
        actual = _git(["rev-parse", "FETCH_HEAD^{commit}"], checkout, timeout)
        if actual != dependency["commit"]:
            raise GitDependencyError(f"Git dependency fetched {actual}, expected {dependency['commit']}")
        _git(["checkout", "--quiet", "--detach", actual], checkout, timeout)
        # Only source bytes are cached, so copies cannot share Git metadata
        # or accidentally use a different revision through Git commands.
        shutil.rmtree(checkout / ".git")
        try:
            checkout.rename(destination)
        except FileExistsError:
            pass  # Another runner finished the same immutable checkout.
    return destination


def stage_git_dependencies(dependencies: list[dict], cache: Path, destination: Path, timeout: float) -> None:
    """Fresh repository copies plus a NAME-indexed workspace for AIHC/GHC.

    Keep each repository's layout intact for relative source/include paths.
    Only explicitly selected packages are exposed to dependency resolution.
    """
    validate_git_dependencies(dependencies)
    shutil.rmtree(destination, ignore_errors=True)
    workspace = destination / "workspace"
    workspace.mkdir(parents=True)
    for index, dependency in enumerate(dependencies):
        source = _checkout(dependency, cache, timeout)
        checkout = destination / "repositories" / str(index)
        shutil.copytree(source, checkout, symlinks=True)
        for name, subdir in dependency["packages"].items():
            package = (checkout / subdir).resolve()
            if not package.is_relative_to(checkout.resolve()):
                raise GitDependencyError(f"Git dependency {name}: subdirectory escapes the repository")
            cabal_files = list(package.glob("*.cabal"))
            if len(cabal_files) != 1:
                raise GitDependencyError(f"Git dependency {name}: expected one .cabal file in {subdir}")
            declared = re.search(r"(?im)^name\s*:\s*(\S+)\s*$", cabal_files[0].read_text(encoding="utf-8"))
            if not declared or declared[1] != name:
                raise GitDependencyError(f"Git dependency {name}: package name does not match {cabal_files[0].name}")
            (workspace / name).symlink_to(package, target_is_directory=True)
