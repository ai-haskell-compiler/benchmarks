import os
import sys
import tempfile
import unittest
from pathlib import Path

from aihc_bench.process import run_measured


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
