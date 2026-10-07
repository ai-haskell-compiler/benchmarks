"""Cabal's Hackage package list, and whether it reaches the benchmarks' pins.

A benchmark's ``cabal.project.freeze`` pins the moment of the Hackage index it
was solved against, and cabal refuses to resolve against an index older than
that pin. A machine whose last ``cabal update`` predates the pin cannot build
the GHC side of that benchmark at all, and since every AIHC number is a ratio
against GHC, that takes the whole benchmark with it. The MicroHs benchmark
landed with a pin three weeks newer than the package list on two workers, and
both stopped measuring until somebody ran ``cabal update`` by hand.

The list is refreshed here, before anything is timed, rather than left to the
operator: ``doctor`` had been saying the index was fine because cabal writes
the word ``HEAD`` into ``01-index.timestamp`` and the age check gave up on it
silently.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .process import run_command

#: ``index-state:`` in a benchmark's freeze file.
FREEZE_INDEX_STATE = re.compile(r"(?m)^index-state:\s*hackage\.haskell\.org\s+(\S+)")


def newest_freeze_index_state(config: Dict[str, Any], root: Path) -> Tuple[Optional[str], Optional[str]]:
    """The newest index-state any benchmark pins, and which benchmark pins it."""
    newest: Optional[str] = None
    owner: Optional[str] = None
    for benchmark in config.get("benchmarks", []):
        freeze = root / benchmark["source"] / "cabal.project.freeze"
        if not freeze.is_file():
            continue
        found = FREEZE_INDEX_STATE.search(freeze.read_text(encoding="utf-8"))
        if found and (newest is None or found.group(1) > newest):
            newest, owner = found.group(1), benchmark["id"]
    return newest, owner


def index_state_reached(index: Path) -> Optional[str]:
    """How far the machine's package list reaches, as an index-state.

    Cabal writes ``01-index.timestamp`` beside the tarball. It holds a Unix
    time when ``cabal update`` was given an explicit index-state and the word
    ``HEAD`` when it was not, which is the usual case -- so the marker alone
    said nothing about three workers whose lists were weeks old, and the
    tarball's own modification time is what answers then. Neither file is a
    failure to find: an index that resolves is the thing that matters, and
    the build says so plainly if it does not.
    """
    marker = index.with_name("01-index.timestamp")
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text.isdigit():
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(text)))
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(index.stat().st_mtime))
    except OSError:
        return None


def package_list(root: Path) -> Optional[Path]:
    """The Hackage package list ``cabal build`` resolves dependencies against.

    ``cabal path`` is asked rather than assuming ``~/.cache/cabal``, since
    ``CABAL_DIR`` and the XDG layout both move it.
    """
    try:
        process = run_command(["cabal", "path", "--cache-home"], root, 60.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if process.returncode != 0:
        return None
    cache_home = process.stdout.strip().splitlines()
    if not cache_home:
        return None
    return Path(cache_home[-1]) / "packages" / "hackage.haskell.org" / "01-index.tar"


def stale_package_list(config: Dict[str, Any], root: Path) -> Optional[str]:
    """Why the package list cannot serve a benchmark, or ``None`` when it can.

    Missing is stale too: cabal builds nothing without it.
    """
    wanted, benchmark = newest_freeze_index_state(config, root)
    if not wanted:
        return None
    index = package_list(root)
    if index is None:
        return "could not ask cabal for its cache directory"
    if not index.is_file() or index.stat().st_size == 0:
        return f"no package list at {index}"
    reached = index_state_reached(index)
    if reached and reached < wanted:
        return f"{benchmark} pins index-state {wanted}, this machine reaches {reached}"
    return None


def refresh_package_list(config: Dict[str, Any], root: Path, timeout_seconds: float) -> Optional[str]:
    """Run ``cabal update`` when a benchmark pins a newer index than the machine has.

    Returns a description when the refresh fails or the list is still behind
    afterwards. That is not fatal here: the GHC compile fails on its own with
    cabal's message, the baseline guard stops the run, and the commit is
    retried once the machine can reach the pin.
    """
    reason = stale_package_list(config, root)
    if reason is None:
        return None
    print(f"refreshing cabal's package list: {reason}")
    try:
        process = run_command(["cabal", "update"], root, timeout_seconds)
    except (OSError, subprocess.SubprocessError) as error:
        return f"cabal update failed: {error}"
    if process.returncode != 0:
        return f"cabal update exited {process.returncode}: {(process.stdout + process.stderr)[-2000:]}"
    return stale_package_list(config, root)
