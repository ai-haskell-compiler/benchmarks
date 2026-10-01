"""Instruction and cycle counts for one benchmark process.

Wall time moves with whatever else the machine is doing; the number of
instructions a program retires barely does, so a change in the compiler shows
up in it even when the timing noise hides it. Both supported platforms keep
those counts per process, but read them in different ways:

- Linux opens one ``perf_event_open`` counter per event on the runner itself,
  disabled, inherited by every child and enabled when a child calls
  ``exec``. The runner never execs, so its own counter stays at zero; each
  child's count is folded back into it when the child exits. The counters are
  user-space only, which ``perf_event_paranoid`` of 2 -- the kernel default --
  permits without privileges.
- macOS keeps ``ri_instructions`` and ``ri_cycles`` in ``proc_pid_rusage``
  and keeps answering for a process that has exited but not been reaped, so
  the counts are read between the exit and ``wait4``. They cover user and
  kernel mode.

A platform or machine without counters measures nothing here rather than
failing: the metrics are recorded ``unavailable``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import select
import struct
import subprocess
import sys
from typing import List, Optional, Tuple

Counts = Tuple[int, int]


class CounterSession:
    """Counts for the next child spawned while the session is open.

    ``wait_exited`` blocks until the child has exited without reaping it;
    ``read`` must come after that and before ``wait4``.
    """

    def wait_exited(self, pid: int) -> None:
        pass

    def read(self, pid: int) -> Optional[Counts]:
        return None

    def close(self) -> None:
        pass

    def __enter__(self) -> "CounterSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_session() -> CounterSession:
    """A session for the next spawned child, or one that counts nothing."""
    try:
        if sys.platform.startswith("linux"):
            return _LinuxSession()
        if sys.platform == "darwin":
            return _DarwinSession()
    except OSError:
        pass
    return CounterSession()


def probe() -> str:
    """Describe whether this machine can count, for ``doctor``.

    A counter that opens is not yet one that counts: a macOS virtual machine
    answers ``proc_pid_rusage`` with zero instructions, so a short child is
    counted for real.
    """
    if sys.platform.startswith("linux"):
        source = "perf_event_open (user space)"
        try:
            session: CounterSession = _LinuxSession()
        except OSError as error:
            return f"unavailable ({error}; perf_event_paranoid must be 2 or lower)"
    elif sys.platform == "darwin":
        source = "proc_pid_rusage"
        try:
            session = _DarwinSession()
        except OSError as error:
            return f"unavailable ({error})"
    else:
        return "unavailable on this platform"
    with session:
        process = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        session.wait_exited(process.pid)
        counts = session.read(process.pid)
        process.wait()
    if counts is None:
        return "unavailable (the counters opened but counted nothing; a virtual machine without a PMU?)"
    return source


# --- Linux ------------------------------------------------------------------

_PERF_TYPE_HARDWARE = 0
_PERF_COUNT_HW_CPU_CYCLES = 0
_PERF_COUNT_HW_INSTRUCTIONS = 1
_PERF_FORMAT_TOTAL_TIME_ENABLED = 1 << 0
_PERF_FORMAT_TOTAL_TIME_RUNNING = 1 << 1
_PERF_FLAG_FD_CLOEXEC = 1 << 3
# perf_event_attr flag bits.
_DISABLED = 1 << 0
_INHERIT = 1 << 1
_EXCLUDE_KERNEL = 1 << 5
_EXCLUDE_HV = 1 << 6
_ENABLE_ON_EXEC = 1 << 12
_SYSCALL_NUMBERS = {"x86_64": 298, "aarch64": 241}
# PERF_ATTR_SIZE_VER0, which every kernel with perf_event_open accepts: type,
# size, config, sample_period, sample_type, read_format, flags,
# wakeup_events, bp_type and config1.
_ATTR = struct.Struct("=IIQQQQQIIQ")


def perf_event_attr(config: int) -> bytes:
    return _ATTR.pack(
        _PERF_TYPE_HARDWARE,
        _ATTR.size,
        config,
        0,
        0,
        _PERF_FORMAT_TOTAL_TIME_ENABLED | _PERF_FORMAT_TOTAL_TIME_RUNNING,
        _DISABLED | _INHERIT | _EXCLUDE_KERNEL | _EXCLUDE_HV | _ENABLE_ON_EXEC,
        0,
        0,
        0,
    )


def scheduled_count(raw: bytes) -> Optional[int]:
    """The count from a ``read`` of one counter, if it was never multiplexed.

    A counter that shared the PMU with others only ran part of the time and
    its count is an extrapolation; that is recorded as no count rather than
    as a number that looks measured.
    """
    value, enabled, running = struct.unpack("=QQQ", raw)
    if running != enabled:
        return None
    return value


class _LinuxSession(CounterSession):
    def __init__(self) -> None:
        number = _SYSCALL_NUMBERS.get(platform.machine())
        if number is None:
            raise OSError(errno.ENOSYS, f"no perf_event_open syscall number for {platform.machine()}")
        libc = ctypes.CDLL(None, use_errno=True)
        self._fds: List[int] = []
        try:
            for config in (_PERF_COUNT_HW_INSTRUCTIONS, _PERF_COUNT_HW_CPU_CYCLES):
                attr = ctypes.create_string_buffer(perf_event_attr(config), _ATTR.size)
                fd = libc.syscall(number, attr, 0, -1, -1, _PERF_FLAG_FD_CLOEXEC)
                if fd < 0:
                    code = ctypes.get_errno()
                    raise OSError(code, f"perf_event_open: {os.strerror(code)}")
                self._fds.append(fd)
        except OSError:
            self.close()
            raise

    def wait_exited(self, pid: int) -> None:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)

    def read(self, pid: int) -> Optional[Counts]:
        # The child's counts reach these counters as it exits, before its
        # parent hears of it, so they are complete once ``wait_exited``
        # returns.
        instructions, cycles = (scheduled_count(os.read(fd, 24)) for fd in self._fds)
        if instructions is None or cycles is None:
            return None
        return instructions, cycles

    def close(self) -> None:
        for fd in self._fds:
            os.close(fd)
        self._fds = []


# --- macOS ------------------------------------------------------------------

_RUSAGE_INFO_V4 = 4
# struct rusage_info_v4: a 16-byte uuid followed by 64-bit fields, of which
# ri_instructions and ri_cycles are the 30th and 31st. The buffer is larger
# than the struct so a newer kernel's additions cannot overrun it.
_RUSAGE_INFO_WORDS = 64
_RI_INSTRUCTIONS = 2 + 29


class _DarwinSession(CounterSession):
    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)

    def wait_exited(self, pid: int) -> None:
        # Python offers no ``waitid`` here, so ``WNOWAIT`` is out of reach;
        # kqueue reports the exit without reaping. A child that exited before
        # the registration answers ESRCH, which says the same thing.
        queue = select.kqueue()
        try:
            event = select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)
            queue.control([event], 1, None)
        except ProcessLookupError:
            pass
        finally:
            queue.close()

    def read(self, pid: int) -> Optional[Counts]:
        buffer = (ctypes.c_uint64 * _RUSAGE_INFO_WORDS)()
        if self._libc.proc_pid_rusage(pid, _RUSAGE_INFO_V4, ctypes.byref(buffer)) != 0:
            return None
        instructions, cycles = buffer[_RI_INSTRUCTIONS], buffer[_RI_INSTRUCTIONS + 1]
        if not instructions or not cycles:
            return None
        return int(instructions), int(cycles)
