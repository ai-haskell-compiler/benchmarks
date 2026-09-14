from __future__ import annotations

import functools
import locale as locale_module
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional

from .stats import StatsError, read_stats_file


@dataclass(frozen=True)
class ProcessMeasurement:
    command: list
    wall_time_ns: int
    cpu_time_ns: int
    peak_rss_bytes: int
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    runtime_stats: Optional[Dict[str, int]] = None
    stats_error: Optional[str] = None
    environment: Dict[str, str] = field(default_factory=dict)


class _AlarmExpired(Exception):
    pass


# Every child process runs under a fixed locale so tool output is byte-stable
# across machines, but that locale must be UTF-8: ``aihc`` writes core files
# containing characters like U+2200 FOR ALL, and a GHC-compiled program under
# ``LC_ALL=C`` gets ASCII output handles and dies with
# ``commitAndReleaseBuffer: invalid argument``.  ``C.UTF-8`` is the neutral
# choice and exists on glibc; macOS needs one of the fallbacks.
_UTF8_LOCALES = ("C.UTF-8", "C.utf8", "en_US.UTF-8", "en_US.utf8")


@functools.lru_cache(maxsize=1)
def utf8_locale() -> Optional[str]:
    """The most neutral UTF-8 locale this machine supports, or ``None``.

    Probing with ``setlocale`` is what the child's libc will do, so a name
    accepted here is a name the child accepts too.  The process locale is
    restored before returning; ``None`` means no candidate was supported and
    the caller should surface that rather than silently fall back to ``C``.
    """
    previous = locale_module.setlocale(locale_module.LC_CTYPE)
    try:
        for candidate in _UTF8_LOCALES:
            try:
                locale_module.setlocale(locale_module.LC_CTYPE, candidate)
            except locale_module.Error:
                continue
            return candidate
        return None
    finally:
        locale_module.setlocale(locale_module.LC_CTYPE, previous)


def _base_environment() -> Dict[str, str]:
    """A child environment with a deterministic, UTF-8 locale."""
    environment = os.environ.copy()
    environment["LC_ALL"] = utf8_locale() or "C.UTF-8"
    return environment



def run_measured(
    command: Iterable[str],
    cwd: Path,
    timeout_seconds: float,
    environment_overrides: Optional[Dict[str, str]] = None,
    stats_file: Optional[str] = None,
    stats_format: Optional[str] = None,
) -> ProcessMeasurement:
    """Run one benchmark process and measure it through ``wait4``.

    ``stats_file`` names a file the process may write runtime statistics to.
    It is removed before the process starts, so a file present afterwards was
    written by this invocation. A process that writes nothing is measured
    without runtime statistics rather than failing.
    """
    argv = list(command)
    if not argv:
        raise ValueError("cannot run an empty command")
    environment = _base_environment()
    environment.update(environment_overrides or {})
    if stats_file:
        try:
            os.unlink(stats_file)
        except FileNotFoundError:
            pass

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        start = time.perf_counter_ns()
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            env=environment,
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
    cpu_time_ns = round((usage.ru_utime + usage.ru_stime) * 1_000_000_000) if usage else 0

    runtime_stats = None
    stats_error = None
    if stats_file and not timed_out and exit_code == 0:
        try:
            runtime_stats = read_stats_file(stats_file, stats_format)
        except StatsError as error:
            stats_error = str(error)
    if stats_file:
        try:
            os.unlink(stats_file)
        except FileNotFoundError:
            pass

    return ProcessMeasurement(
        command=argv,
        wall_time_ns=finish - start,
        cpu_time_ns=cpu_time_ns,
        peak_rss_bytes=peak_rss,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        runtime_stats=runtime_stats,
        stats_error=stats_error,
        environment=dict(environment_overrides or {}),
    )


def run_command(
    command: Iterable[str],
    cwd: Path,
    timeout_seconds: float,
    environment_overrides: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    environment = _base_environment()
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
