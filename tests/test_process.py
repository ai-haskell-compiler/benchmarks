import os
import sys
import tempfile
import unittest
from pathlib import Path

from aihc_bench import counters
from aihc_bench.counters import CounterSession
from aihc_bench.process import run_command, run_measured, utf8_locale


@unittest.skipUnless(hasattr(__import__("os"), "wait4"), "wait4 is required")
class ProcessTests(unittest.TestCase):
    def test_captures_complete_process_and_rusage(self):
        result = run_measured([sys.executable, "-c", "print('ok')"], Path("."), 2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"ok\n")
        self.assertGreater(result.wall_time_ns, 0)
        self.assertGreater(result.cpu_time_ns, 0)
        self.assertGreater(result.peak_rss_bytes, 0)
        self.assertIsNone(result.runtime_stats)

    def test_timeout_kills_the_process_group(self):
        result = run_measured([sys.executable, "-c", "import time; time.sleep(2)"], Path("."), 0.05)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIsNone(result.instructions)

    def test_counts_are_read_after_exit_and_before_reaping(self):
        calls = []

        class Recording(CounterSession):
            def wait_exited(self, pid):
                calls.append("exited")

            def read(self, pid):
                # Not yet reaped, so the pid still names the child.
                calls.append(os.waitpid(pid, os.WNOHANG)[0] == 0 or "reaped")
                return 7, 5

            def close(self):
                calls.append("closed")

        result = run_measured([sys.executable, "-c", "pass"], Path("."), 5, open_counters=Recording)
        self.assertEqual((result.instructions, result.cycles), (7, 5))
        self.assertEqual(calls[0], "exited")
        self.assertEqual(calls[-1], "closed")

    @unittest.skipIf(counters.probe().startswith("unavailable"), "no hardware counters on this machine")
    def test_counts_this_machine_s_child(self):
        small = run_measured([sys.executable, "-c", "pass"], Path("."), 10)
        large = run_measured([sys.executable, "-c", "sum(range(3_000_000))"], Path("."), 10)
        self.assertGreater(small.instructions, 0)
        self.assertGreater(small.cycles, 0)
        self.assertGreater(large.instructions, small.instructions + 10_000_000)

    def test_reads_stats_written_by_the_process_and_removes_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            stats_file = os.path.join(directory, "run.stats")
            script = (
                "import json, os; open(os.environ['AIHC_RTS_STATS'], 'w').write(json.dumps("
                "{'schema': 1, 'peak_heap_bytes': 1, 'allocated_bytes': 2, 'gc_count': 3, 'gc_time_ns': 4}))"
            )
            result = run_measured(
                [sys.executable, "-c", script],
                Path("."),
                5,
                environment_overrides={"AIHC_RTS_STATS": stats_file},
                stats_file=stats_file,
                stats_format="aihc",
            )
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(result.runtime_stats, {"peak_heap_bytes": 1, "allocated_bytes": 2, "gc_count": 3, "gc_time_ns": 4})
            self.assertFalse(os.path.exists(stats_file))

    def test_malformed_stats_are_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            stats_file = os.path.join(directory, "run.stats")
            result = run_measured(
                [sys.executable, "-c", f"open({stats_file!r}, 'w').write('garbage')"],
                Path("."),
                5,
                stats_file=stats_file,
                stats_format="ghc",
            )
            self.assertEqual(result.exit_code, 0)
            self.assertIsNone(result.runtime_stats)
            self.assertIn("pair list", result.stats_error)


if __name__ == "__main__":
    unittest.main()


class LocaleTests(unittest.TestCase):
    """The child locale must be deterministic *and* UTF-8.

    ``aihc`` writes core files containing U+2200; under ``LC_ALL=C`` a
    GHC-compiled program gets ASCII output handles and fails with
    ``commitAndReleaseBuffer: invalid argument``, which is how every AIHC
    configuration silently failed to compile.

    Python is not a faithful stand-in for GHC here -- PEP 538 quietly coerces
    the C locale to C.UTF-8 -- so the children below disable that coercion and
    UTF-8 mode, leaving the interpreter to honour the locale the way a
    GHC-compiled binary does.
    """

    #: Defeat PEP 538 coercion and PEP 540 UTF-8 mode in the child.
    _HONOUR_LOCALE = {"PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}

    _WRITE_FOR_ALL = "import sys; sys.stdout.write('\\u2200')"

    def test_a_utf8_locale_is_available(self):
        self.assertIsNotNone(utf8_locale(), "no UTF-8 locale is supported on this machine")

    def test_children_can_write_non_ascii(self):
        result = run_command(
            [sys.executable, "-c", self._WRITE_FOR_ALL], Path("."), 30, dict(self._HONOUR_LOCALE)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\u2200")

    def test_measured_children_can_write_non_ascii(self):
        result = run_measured(
            [sys.executable, "-c", self._WRITE_FOR_ALL], Path("."), 30, dict(self._HONOUR_LOCALE)
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8"), "\u2200")

    def test_the_locale_is_pinned_rather_than_inherited(self):
        program = "import os; print(os.environ['LC_ALL'])"
        result = run_command([sys.executable, "-c", program], Path("."), 30)
        self.assertEqual(result.stdout.strip(), utf8_locale())
