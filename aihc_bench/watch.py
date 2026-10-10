"""``./bench``: measure one commit after another, and say what is happening.

The watcher is the interactive counterpart of the worker services in
``deploy/``. It plans the next commit and says why it was chosen, estimates
how long that commit and the rest of the history will take from this
machine's uploaded results, and starts each commit as its own ``run``
process once the machine may measure: inside a time window when windows are
set (see ``schedule``), on mains power, and with no other run on the machine.

It follows both repositories. The compiler's history is fetched every poll
and replanned, so a commit that lands is planned straight away. The suite's
own checkout is fast-forwarded to its upstream between commits, and when the
checkout changes -- a pull, a local commit, an edit to a tracked file -- the
watcher restarts itself through ``nix run`` so the change is what measures
the next commit. A commit already being measured is never interrupted for
either: it finishes, and the change applies after it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .database import Database
from .planner import build_plan, merge_terminal_attempts
from .schedule import Schedule, Window, current_window_end, in_window, next_window_start, on_battery

#: How many recent commits the duration estimate is the median of.
ESTIMATE_SAMPLE = 10

METRIC_NAMES = {"wall_time": "wall time", "allocated_bytes": "allocations"}


# ---------------------------------------------------------------------------
# One run at a time


class RunBusy(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold the machine's run lock, or raise RunBusy naming who holds it.

    Two runs on one machine measure each other's contention, which is how
    four published commits were spoiled once already.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunBusy(f"another benchmark run holds {path} (pid {_lock_pid(path) or 'unknown'})") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        handle.close()


def lock_holder(path: Path) -> Optional[str]:
    """The pid holding the run lock, "unknown" when unreadable, None when free."""
    if not path.exists():
        return None
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _lock_pid(path) or "unknown"
        fcntl.flock(handle, fcntl.LOCK_UN)
    return None


def _lock_pid(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def other_runs(listing: Optional[str] = None) -> List[str]:
    """Pids of ``aihc_bench run`` processes, which an older runner starts without the lock."""
    if listing is None:
        try:
            listing = subprocess.run(
                ["ps", "-eo", "pid=,args="], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False
            ).stdout
        except OSError:
            return []
    pids = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == str(os.getpid()):
            continue
        tokens = parts[1:]
        for index, token in enumerate(tokens):
            if (token == "aihc_bench" and index and tokens[index - 1] == "-m") or os.path.basename(token) == "aihc-bench":
                if "run" in tokens[index + 1 :]:
                    pids.append(parts[0])
                break
    return pids


# ---------------------------------------------------------------------------
# Why the next commit is next


def explain(plan: Dict[str, Any], ref: str = "origin/main", since: Optional[str] = None) -> List[str]:
    """Say which commit is next and why, as a headline and optional detail lines."""
    commit = plan.get("next")
    if commit is None:
        return ["nothing to benchmark: every commit has a result"]
    lead = f"commit {commit['sha'][:12]} from {commit['committed_at'][:10]} is next because"
    if plan["stage"] == "head":
        return [f"{lead} it is HEAD, the newest commit on {ref}, and has no result yet"]
    if plan["stage"] == "first":
        window = f" (since {since})" if since else ""
        return [f"{lead} it is TAIL, the oldest commit in the benchmark window{window}, and has no result yet"]
    gap = plan["gaps"][0]
    left = gap["left"]["sha"][:12] if gap.get("left") else "the start"
    right = gap["right"]["sha"][:12] if gap.get("right") else "the end"
    span = f"it splits the {gap['width']} unmeasured commit{'s' if gap['width'] != 1 else ''} between {left} and {right}"
    change = gap.get("change")
    if not change:
        return [f"{lead} {span}, the widest gap left; its ends show no difference"]
    percent = (change["right"] / change["left"] - 1) * 100
    metric = METRIC_NAMES.get(change["metric"], change["metric"])
    return [
        f"{lead} of a {abs(percent):.1f}% {metric} difference between its neighbours {left} and {right}",
        f"{change['benchmark']} {change['configuration']}: {format_metric(change['metric'], change['left'])}"
        f" -> {format_metric(change['metric'], change['right'])}; {span}",
    ]


def format_metric(metric: str, value: float) -> str:
    if metric in {"wall_time", "compile_time"}:
        seconds = value / 1e9
        return f"{seconds * 1000:.1f} ms" if seconds < 1 else f"{seconds:.2f} s"
    if metric.endswith("bytes") or metric == "artifact_size":
        for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
            if value >= scale:
                return f"{value / scale:.1f} {unit}"
        return f"{value:.0f} B"
    return f"{value:g}"


# ---------------------------------------------------------------------------
# How long it will take


def recent_commit_seconds(database: Database, limit: int = ESTIMATE_SAMPLE) -> List[float]:
    """Wall clock of the newest commits this machine measured and uploaded, newest first.

    Every benchmark of a commit carries the same timing record, so each
    commit counts once. Inherited results measured nothing and are skipped.
    """
    rows = database.connection.execute(
        "SELECT commit_sha, json_extract(result_json, '$.timing.total_ns') AS total FROM attempts "
        "WHERE status = 'complete' AND inherited_from IS NULL AND uploaded_at IS NOT NULL AND total IS NOT NULL "
        "ORDER BY finished_at DESC"
    ).fetchall()
    seen, seconds = set(), []
    for row in rows:
        if row["commit_sha"] in seen:
            continue
        seen.add(row["commit_sha"])
        seconds.append(row["total"] / 1e9)
        if len(seconds) >= limit:
            break
    return seconds


def published_commit_seconds(server_url: str, machine_id: str) -> Optional[float]:
    """This machine's last commit as the site has it, for a checkout with no local history."""
    request = urllib.request.Request(f"{server_url.rstrip('/')}/api/overview", headers={"User-Agent": "aihc-bench"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            overview = json.load(response)
    except (OSError, ValueError):
        return None
    for machine in overview.get("machines", []):
        if machine.get("machine_id") == machine_id:
            total = (machine.get("last_commit") or {}).get("total_ns")
            return total / 1e9 if total else None
    return None


def remaining_commits(commits: List[Dict[str, Any]], terminal: List[Dict[str, Any]]) -> int:
    """Commits still to be measured, counting each compiler tree once.

    A commit that builds the same compiler as one already measured inherits
    its result, so only one commit per unmeasured tree costs a measurement.
    """
    done = {attempt["commit_sha"] for attempt in terminal}
    covered = {commit["tree_key"] for commit in commits if commit["sha"] in done and commit.get("tree_key")}
    pending, count = set(), 0
    for commit in commits:
        key = commit.get("tree_key")
        if commit["sha"] in done or (key and (key in covered or key in pending)):
            continue
        if key:
            pending.add(key)
        count += 1
    return count


def finish_time(start: datetime, count: int, seconds: float, windows: List[Window]) -> datetime:
    """When ``count`` commits of ``seconds`` each are done, starting each only inside a window."""
    moment = start
    for _ in range(count):
        moment = next_window_start(windows, moment) or moment
        moment += timedelta(seconds=seconds)
    return moment


def format_duration(seconds: float) -> str:
    """``1h47m``, ``2d 7h`` or ``12m``: a duration as a person would say it."""
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def format_clock(seconds: float) -> str:
    """``3:12:05``: a countdown."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"


def format_moment(moment: datetime, now: datetime) -> str:
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if moment.date() == (now + timedelta(days=1)).date():
        return moment.strftime("tomorrow %H:%M")
    return moment.strftime("%a %d %b %H:%M")


# ---------------------------------------------------------------------------
# The suite's own checkout


class Checkout:
    """The benchmark suite checkout the watcher runs from.

    Its fingerprint is HEAD plus the diff of tracked files: what ``nix run``
    sees of a Git flake, untracked files being invisible to it.
    """

    def __init__(self, root: Path):
        self.root = root
        self.initial = self.fingerprint()
        self.status = ""

    def _git(self, *arguments: str) -> Tuple[int, str]:
        try:
            process = subprocess.run(
                ["git", "-C", str(self.root), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return 1, str(error)
        return process.returncode, (process.stdout if process.returncode == 0 else process.stderr).strip()

    def fingerprint(self) -> Optional[str]:
        code, head = self._git("rev-parse", "HEAD")
        if code != 0:
            return None
        try:
            diff = subprocess.run(
                ["git", "-C", str(self.root), "diff", "HEAD", "--binary"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False
            ).stdout
        except OSError:
            return None
        return f"{head}:{hashlib.sha256(diff).hexdigest()}"

    def changed(self) -> bool:
        return self.initial is not None and self.fingerprint() != self.initial

    def dirty(self) -> bool:
        code, output = self._git("status", "--porcelain")
        return code == 0 and bool(output)

    def poll(self, apply: bool) -> Optional[str]:
        """Fetch the upstream and, when ``apply``, fast-forward to it.

        Returns a message worth printing when something happened. A checkout
        with commits of its own is somebody's work in progress and is never
        merged into; it only restarts on its own changes.
        """
        code, branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        code, upstream = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if code != 0:
            self.status = f"{branch}, no upstream: restarting on local changes only"
            return None
        code, error = self._git("fetch", "--quiet")
        if code != 0:
            self.status = f"{branch}, could not fetch {upstream}"
            return f"could not fetch the suite's upstream {upstream}: {error.splitlines()[0] if error else 'git fetch failed'}"
        _, counts = self._git("rev-list", "--left-right", "--count", "HEAD...@{u}")
        ahead, behind = (int(part) for part in counts.split()) if counts else (0, 0)
        if behind and ahead:
            self.status = f"{branch}, {ahead} local and {behind} upstream commits: not following {upstream}"
            return None
        if behind and not apply:
            self.status = f"{branch}, {behind} commit{'s' if behind != 1 else ''} behind {upstream}: applied after this commit"
            return None
        if behind:
            code, error = self._git("merge", "--ff-only", "--quiet", "@{u}")
            if code != 0:
                self.status = f"{branch}, cannot fast-forward to {upstream}"
                return f"cannot fast-forward the suite to {upstream}: {error.splitlines()[0] if error else 'merge failed'}"
            self.status = f"{branch}, up to date with {upstream}"
            return f"suite updated to {upstream} ({behind} new commit{'s' if behind != 1 else ''})"
        self.status = f"{branch}, {ahead} local commit{'s' if ahead != 1 else ''} ahead of {upstream}" if ahead else f"{branch}, up to date with {upstream}"
        return None


# ---------------------------------------------------------------------------
# The terminal


class Display:
    """Blocks of information and one live status line beneath them.

    On a terminal the status line is redrawn in place every second. Anywhere
    else (a service's log) it is printed only when its meaning changes, so a
    countdown does not write a line a second.
    """

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self.current = ""
        self.logged_key: Optional[str] = None

    def _clear(self) -> None:
        if self.tty and self.current:
            self.stream.write("\r\033[K")

    def _draw(self) -> None:
        if self.tty and self.current:
            width = shutil.get_terminal_size((100, 20)).columns or 100
            self.stream.write(self.current[: max(width - 1, 20)])
        self.stream.flush()

    def close(self) -> None:
        """Leave the cursor below the status line."""
        if self.tty and self.current:
            self.stream.write("\n")
            self.stream.flush()
            self.current = ""

    def lines(self, lines: List[str]) -> None:
        self._clear()
        for line in lines:
            self.stream.write(line + "\n")
        self._draw()

    def status(self, text: str, key: Optional[str] = None) -> None:
        """Show ``text``; ``key`` says when a non-terminal should log it again."""
        if self.tty:
            self._clear()
            self.current = text
            self._draw()
        elif (key or text) != self.logged_key:
            self.logged_key = key or text
            self.stream.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
            self.stream.flush()

    def bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.tty else text

    def dim(self, text: str) -> str:
        return f"\033[2m{text}\033[0m" if self.tty else text


# ---------------------------------------------------------------------------
# The loop


class Watcher:
    def __init__(
        self,
        *,
        root: Path,
        config: Dict[str, Any],
        database: Database,
        experiments: Dict[str, str],
        platform_id: str,
        machine_id: str,
        refresh_history: Callable[[bool], List[Dict[str, Any]]],
        run_command: List[str],
        restart_command: List[str],
        poll_seconds: float = 300,
        allow_battery: bool = False,
        display: Optional[Display] = None,
    ):
        self.root = root
        self.config = config
        self.database = database
        self.experiments = experiments
        self.platform_id = platform_id
        self.machine_id = machine_id
        self.refresh_history = refresh_history
        self.run_command = run_command
        self.restart_command = restart_command
        self.poll_seconds = poll_seconds
        self.allow_battery = allow_battery
        self.display = display or Display()
        self.schedule = Schedule(database.path.parent / "schedule.json")
        self.lock_path = database.path.parent / "run.lock"
        self.checkout = Checkout(root)
        self.snapshot: Dict[str, Any] = {}
        self.printed: List[str] = []
        self.next_poll = 0.0
        self.next_change_check = 0.0
        self.failures = 0
        self.retry_at = 0.0
        self.battery: Optional[bool] = None
        self.battery_checked = 0.0
        self.published_seconds: Optional[float] = None
        self.published_checked = False

    # -- state ---------------------------------------------------------------

    def refresh(self, fetch: bool = True) -> None:
        """Re-read the compiler history and the results, and replan."""
        history = self.refresh_history(fetch)
        by_experiment = {
            experiment: self.database.terminal_attempts(experiment, self.platform_id) for experiment in self.experiments.values()
        }
        terminal = merge_terminal_attempts(by_experiment)
        commits = self.database.commits()
        plan = build_plan(commits, terminal)
        durations = recent_commit_seconds(self.database)
        if durations:
            seconds: Optional[float] = statistics.median(durations)
            source = f"median of the last {len(durations)} commit{'s' if len(durations) != 1 else ''} this machine uploaded"
        else:
            if not self.published_checked:
                self.published_seconds = published_commit_seconds(self.config["publishing"]["server_url"], self.machine_id)
                self.published_checked = True
            seconds = self.published_seconds
            source = "the last commit this machine published" if seconds else ""
        measured = sum(1 for attempt in terminal if not attempt.get("inherited_from"))
        self.snapshot = {
            "history": history,
            "terminal": len(terminal),
            "measured": measured,
            "inherited": len(terminal) - measured,
            "plan": plan,
            "remaining": remaining_commits(commits, terminal),
            "seconds": seconds,
            "source": source,
        }

    def windows(self) -> Tuple[List[Window], Optional[str]]:
        try:
            return self.schedule.windows(), None
        except ValueError as error:
            return [], str(error)

    def on_battery(self) -> bool:
        if self.allow_battery:
            return False
        if time.monotonic() - self.battery_checked > 15:
            self.battery = on_battery()
            self.battery_checked = time.monotonic()
        return bool(self.battery)

    def gate(self) -> Optional[Tuple[str, str]]:
        """Why no commit may start now, as (key, status line), or None to start one."""
        now = datetime.now()
        if self.snapshot.get("plan", {}).get("next") is None:
            return "caught-up", f"every commit has a result; looking for new ones in {format_clock(self.next_poll - time.monotonic())}"
        if self.retry_at > time.monotonic():
            return "retry", f"the last run failed; retrying in {format_clock(self.retry_at - time.monotonic())}"
        holder = lock_holder(self.lock_path)
        others = other_runs()
        if holder or others:
            return "busy", f"another benchmark run is in progress on this machine (pid {holder or ', '.join(others)}); waiting for it"
        if self.on_battery():
            return "battery", "on battery power; waiting for the charger (or start with --allow-battery)"
        windows, error = self.windows()
        if error:
            return "schedule", f"cannot read the schedule: {error}"
        if not in_window(windows, now):
            opens = next_window_start(windows, now)
            return "window", f"outside the benchmark window; next benchmark starts in {format_clock((opens - now).total_seconds())} (at {format_moment(opens, now)})"
        return None

    # -- what is shown -------------------------------------------------------

    def info(self) -> List[str]:
        d = self.display
        snapshot = self.snapshot
        now = datetime.now()
        plan = snapshot["plan"]
        windows, error = self.windows()
        lines = [
            d.bold(f"aihc-bench · {self.machine_id} · {self.platform_id}"),
            f"suite      {self.checkout.status or 'not a Git checkout'}",
        ]
        if self.checkout.dirty():
            lines.append(d.dim("           uncommitted changes: a benchmark they touch is filed under a new experiment id"))
        lines.append(
            f"coverage   {snapshot['terminal']}/{len(snapshot['history'])} commits have results "
            f"({snapshot['measured']} measured, {snapshot['inherited']} inherited)"
        )
        reason = explain(plan, self.config.get("aihc_ref", "origin/main"), self.config.get("aihc_since"))
        commit = plan.get("next")
        lines.append(f"next       {reason[0]}")
        if commit:
            lines.append(d.dim(f"           \"{commit['subject']}\""))
        lines.extend(d.dim(f"           {line}") for line in reason[1:])
        seconds = snapshot["seconds"]
        if commit and seconds:
            lines.append(f"estimate   ~{format_duration(seconds)} for this commit ({snapshot['source']})")
            remaining = snapshot["remaining"]
            start = next_window_start(windows, now) or now
            done = finish_time(now, remaining, seconds, windows)
            lines.append(
                f"           ~{remaining} commit{'s' if remaining != 1 else ''} left to measure: "
                f"~{format_duration(remaining * seconds)} of benchmarking, done around {format_moment(done, now)}"
            )
            end = current_window_end(windows, start)
            if end and start + timedelta(seconds=seconds) > end:
                lines.append(
                    d.dim(f"           a commit started at {format_moment(start, now)} runs ~{format_duration((start + timedelta(seconds=seconds) - end).total_seconds())} past the window's end; it is allowed to finish")
                )
        elif commit:
            lines.append("estimate   none yet: this machine has uploaded no measured commit")
        if error:
            lines.append(f"window     cannot read the schedule: {error}")
        elif windows:
            lines.append(f"window     {', '.join(map(str, windows))} local time (change with ./bench window)")
        else:
            lines.append("window     any time (add one with ./bench window add 22:00-06:00)")
        battery = on_battery() if not self.allow_battery else None
        lines.append(f"power      {'battery' if battery else 'mains' if battery is False else 'unknown'}{' (battery allowed)' if self.allow_battery else ''}")
        return lines

    def show_info(self, force: bool = False) -> None:
        lines = self.info()
        if force or lines != self.printed:
            self.printed = lines
            self.display.lines([""] + lines)

    # -- running -------------------------------------------------------------

    def restart(self, why: str) -> None:
        self.display.lines([f"restarting: {why}"])
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvp(self.restart_command[0], self.restart_command)

    def poll(self, apply: bool) -> None:
        message = self.checkout.poll(apply)
        if message:
            self.display.lines([message])
        if apply and self.checkout.changed():
            self.restart("the suite checkout changed")

    def run_one(self) -> int:
        """Measure one commit in a ``run`` process, streaming what it prints."""
        snapshot = self.snapshot
        commit = snapshot["plan"]["next"]
        started = time.monotonic()
        begun = datetime.now()
        seconds = snapshot["seconds"]
        eta = f", done around {format_moment(begun + timedelta(seconds=seconds), begun)}" if seconds else ""
        self.display.lines([f"starting {commit['sha'][:12]} at {begun:%H:%M}{eta}"])
        environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            self.run_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=environment
        )
        output: "queue.Queue[Optional[str]]" = queue.Queue()

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output.put(line.rstrip("\n"))
            output.put(None)

        threading.Thread(target=pump, daemon=True).start()
        next_poll = time.monotonic() + self.poll_seconds
        finished = False
        try:
            while not finished:
                try:
                    line = output.get(timeout=1)
                    while True:
                        if line is None:
                            finished = True
                            break
                        self.display.lines([line])
                        line = output.get_nowait()
                except queue.Empty:
                    pass
                elapsed = time.monotonic() - started
                left = f" · ~{format_duration(max(seconds - elapsed, 0))} left" if seconds else ""
                self.display.status(f"benchmarking {commit['sha'][:12]} · {format_clock(elapsed)} elapsed{left}", key="running")
                if time.monotonic() >= next_poll:
                    # Fetch only: the running commit reads the checkout.
                    message = self.checkout.poll(apply=False)
                    if message:
                        self.display.lines([message])
                    next_poll = time.monotonic() + self.poll_seconds
        except KeyboardInterrupt:
            self.display.lines(["interrupted; waiting for the run to stop (the commit is not recorded and will be measured again)"])
            try:
                process.wait()
            except KeyboardInterrupt:
                process.kill()
            raise
        code = process.wait()
        self.display.lines([f"run finished in {format_duration(time.monotonic() - started)} (exit {code})"])
        return code

    def loop(self, once: bool = False) -> None:
        try:
            self._loop(once)
        finally:
            self.display.close()

    def _loop(self, once: bool) -> None:
        self.poll(apply=True)
        self.refresh()
        self.next_poll = time.monotonic() + self.poll_seconds
        self.show_info(force=True)
        if once:
            return
        while True:
            if time.monotonic() >= self.next_poll:
                self.poll(apply=True)
                self.refresh()
                self.next_poll = time.monotonic() + self.poll_seconds
                self.show_info()
            elif time.monotonic() >= self.next_change_check:
                self.next_change_check = time.monotonic() + 5
                if self.checkout.changed():
                    self.restart("the suite checkout changed")
            blocked = self.gate()
            if blocked:
                key, text = blocked
                self.display.status(text, key=key)
                time.sleep(1)
                continue
            self.show_info()
            code = self.run_one()
            if code == 0:
                self.failures = 0
            else:
                # Exit 2 is a machine fault: nothing was recorded and the
                # commit is retried. Back off rather than rebuild against the
                # same fault in a tight loop.
                self.failures += 1
                delay = min(60 * 2 ** (self.failures - 1), 1800)
                self.retry_at = time.monotonic() + delay
                self.display.lines([f"the run exited {code}; retrying in {format_duration(delay)}"])
            self.poll(apply=True)
            self.refresh()
            self.next_poll = time.monotonic() + self.poll_seconds
            self.show_info(force=True)
