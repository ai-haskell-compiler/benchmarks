from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional


@dataclass(frozen=True)
class ProcessMeasurement:
    command: list
    wall_time_ns: int
    peak_rss_bytes: int
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class _AlarmExpired(Exception):
    pass


def run_measured(command: Iterable[str], cwd: Path, timeout_seconds: float) -> ProcessMeasurement:
    argv = list(command)
    if not argv:
        raise ValueError("cannot run an empty command")

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        start = time.perf_counter_ns()
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        timed_out = False
        usage = None
        status = 0
        previous_handler = signal.getsignal(signal.SIGALRM)

        def expire(_signum: int, _frame: object) -> None:
            raise _AlarmExpired()

        try:
            signal.signal(signal.SIGALRM, expire)
            signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
            _, status, usage = os.wait4(process.pid, 0)
        except _AlarmExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, status, usage = os.wait4(process.pid, 0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

        finish = time.perf_counter_ns()
        exit_code = os.waitstatus_to_exitcode(status)
        process.returncode = exit_code
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()

    peak_rss = int(usage.ru_maxrss) if usage else 0
    if not sys.platform.startswith(("darwin", "freebsd")):
        peak_rss *= 1024
    return ProcessMeasurement(
        command=argv,
        wall_time_ns=finish - start,
        peak_rss_bytes=peak_rss,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )


def run_command(
    command: Iterable[str],
    cwd: Path,
    timeout_seconds: float,
    environment_overrides: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    environment.update(environment_overrides or {})
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        env=environment,
        check=False,
    )
