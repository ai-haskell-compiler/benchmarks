import sys
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
        self.assertGreater(result.peak_rss_bytes, 0)

    def test_timeout_kills_the_process_group(self):
        result = run_measured([sys.executable, "-c", "import time; time.sleep(2)"], Path("."), 0.05)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
