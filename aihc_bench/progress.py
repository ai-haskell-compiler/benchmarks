"""Where a running commit is, for whoever is watching it.

``run`` says nothing between "benchmarking" and "recorded", and a commit
takes hours, so a watcher could only say how long it had been. When
``AIHC_BENCH_PROGRESS`` names a file, the runner keeps it up to date with the
phase it is in and, while compiling and measuring, how many cells it has done
of how many. ``./bench`` sets it and reads it every second; without it,
reporting does nothing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

ENVIRONMENT = "AIHC_BENCH_PROGRESS"

_phase: Optional[str] = None
_phase_started = 0.0


def report(phase: str, done: Optional[int] = None, total: Optional[int] = None, item: Optional[str] = None) -> None:
    """Record that the run is in ``phase``, with ``done`` of ``total`` cells finished."""
    global _phase, _phase_started
    target = os.environ.get(ENVIRONMENT)
    if not target:
        return
    if phase != _phase:
        _phase, _phase_started = phase, time.time()
    record = {"phase": phase, "phase_started": _phase_started, "done": done, "total": total, "item": item}
    path = Path(target)
    incoming = path.with_name(path.name + ".incoming")
    try:
        incoming.write_text(json.dumps(record))
        incoming.replace(path)
    except OSError:
        # Progress is a courtesy; a full disk must not end the measurement.
        pass


def read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
