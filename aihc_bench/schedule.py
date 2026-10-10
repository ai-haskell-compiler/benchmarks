"""When a benchmark may start: time windows and the power source.

A measurement shares the machine with nothing, so a machine that does other
work during the day is only measured in the hours it is quiet. The windows
are this machine's business rather than the suite's, so they live in
``.state/schedule.json`` beside the machine id instead of in
``benchmark.json``. They are read in local time and may wrap midnight
(``22:00-06:00``). A commit started inside a window is allowed to finish past
its end. No windows means any time.

A laptop on battery is not measured either: macOS and Linux both lower clock
speeds to save power, which moves every timing in a way that afterwards looks
exactly like a change in the compiler.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.IGNORECASE)


class Window:
    """A daily span of local time, ``start`` inclusive, ``end`` exclusive."""

    def __init__(self, start: int, end: int):
        if not (0 <= start < 1440 and 0 <= end < 1440):
            raise ValueError("window times must be within one day")
        if start == end:
            raise ValueError("a window must not start and end at the same time; remove every window to allow any time")
        self.start = start
        self.end = end

    @classmethod
    def parse(cls, text: str) -> "Window":
        """Read ``22:00-06:00``, ``22-6`` or ``10pm-6am``."""
        parts = text.split("-")
        if len(parts) != 2:
            raise ValueError(f"invalid window {text!r}: expected START-END such as 22:00-06:00")
        return cls(_minutes(parts[0], text), _minutes(parts[1], text))

    def __str__(self) -> str:
        return f"{_clock(self.start)}-{_clock(self.end)}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Window) and (self.start, self.end) == (other.start, other.end)

    def __repr__(self) -> str:
        return f"Window({self})"

    def spans(self, around: datetime) -> List[Tuple[datetime, datetime]]:
        """The occurrences of this window that start from the day before ``around`` to two days after."""
        midnight = around.replace(hour=0, minute=0, second=0, microsecond=0)
        length = (self.end - self.start) % 1440
        occurrences = []
        for day in range(-1, 3):
            start = midnight + timedelta(days=day, minutes=self.start)
            occurrences.append((start, start + timedelta(minutes=length)))
        return occurrences


def _minutes(part: str, text: str) -> int:
    match = _TIME.match(part)
    if not match:
        raise ValueError(f"invalid window {text!r}: cannot read {part.strip()!r} as a time of day")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"invalid window {text!r}: {part.strip()!r} is not a 12-hour time")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour == 24 and minute == 0:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid window {text!r}: {part.strip()!r} is not a time of day")
    return hour * 60 + minute


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_window(windows: List[Window], now: datetime) -> bool:
    """Whether ``now`` (naive local time) may start a benchmark."""
    if not windows:
        return True
    return any(start <= now < end for window in windows for start, end in window.spans(now))


def current_window_end(windows: List[Window], now: datetime) -> Optional[datetime]:
    """When the window ``now`` is inside closes, or None outside every window."""
    ends = [end for window in windows for start, end in window.spans(now) if start <= now < end]
    return max(ends) if ends else None


def next_window_start(windows: List[Window], now: datetime) -> Optional[datetime]:
    """The next moment a benchmark may start: ``now`` inside a window, None without windows."""
    if not windows:
        return None
    if in_window(windows, now):
        return now
    return min(start for window in windows for start, _ in window.spans(now) if start > now)


class Schedule:
    """The windows of this machine, kept in ``.state/schedule.json``."""

    def __init__(self, path: Path):
        self.path = path

    def windows(self) -> List[Window]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except ValueError as error:
            raise ValueError(f"{self.path} is not valid JSON: {error}") from error
        return [Window.parse(text) for text in data.get("windows", [])]

    def save(self, windows: List[Window]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        incoming = self.path.with_suffix(".incoming")
        incoming.write_text(json.dumps({"windows": [str(window) for window in windows]}, indent=2) + "\n")
        incoming.replace(self.path)

    def add(self, window: Window) -> List[Window]:
        windows = self.windows()
        if window not in windows:
            windows.append(window)
            self.save(windows)
        return windows

    def remove(self, window: Window) -> List[Window]:
        windows = self.windows()
        if window not in windows:
            raise ValueError(f"no window {window}; the windows are: {', '.join(map(str, windows)) or 'none'}")
        windows.remove(window)
        self.save(windows)
        return windows


def on_battery() -> Optional[bool]:
    """True on battery, False on mains power, None when the machine cannot say.

    A machine without a battery (every desktop and server) reports False.
    """
    if sys.platform == "darwin":
        return _darwin_on_battery()
    if sys.platform.startswith("linux"):
        return _linux_on_battery(Path("/sys/class/power_supply"))
    return None


def _darwin_on_battery() -> Optional[bool]:
    try:
        output = subprocess.run(
            ["pmset", "-g", "batt"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_pmset(output)


def parse_pmset(output: str) -> Optional[bool]:
    """Read the power source from ``pmset -g batt``: "Now drawing from 'AC Power'"."""
    match = re.search(r"drawing from '([^']+)'", output)
    if not match:
        return None
    return match.group(1) == "Battery Power"


def _linux_on_battery(supplies: Path) -> Optional[bool]:
    """Read ``/sys/class/power_supply``.

    A mains adapter that is online settles it. Without one online, a battery
    that is discharging means battery power; a machine with no battery at all
    is on mains.
    """
    if not supplies.is_dir():
        return None
    mains: List[bool] = []
    discharging = False
    batteries = 0
    for supply in sorted(supplies.iterdir()):
        kind = _read(supply / "type")
        if kind == "Mains":
            mains.append(_read(supply / "online") == "1")
        elif kind == "Battery" and _read(supply / "scope") != "Device":
            batteries += 1
            discharging = discharging or _read(supply / "status") == "Discharging"
    if any(mains):
        return False
    if batteries == 0:
        return False
    if mains:
        return True
    return discharging


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None
