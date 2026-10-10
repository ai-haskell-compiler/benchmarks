import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench import progress
from aihc_bench.watch import DiskUsage, directory_sizes, estimate_progress, format_bytes, phase_medians, progress_bar


class ReportTests(unittest.TestCase):
    def test_nothing_is_written_without_the_variable(self):
        with patch.dict(os.environ, {}, clear=True):
            progress.report("compile", 1, 2)

    def test_the_phase_clock_restarts_only_on_a_new_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            with patch.dict(os.environ, {progress.ENVIRONMENT: str(path)}), patch("aihc_bench.progress.time.time", side_effect=[100.0, 200.0]):
                progress.report("compile", 0, 4, "fib aihc-native-O0")
                first = progress.read(path)
                progress.report("compile", 1, 4, "fib aihc-native-O1")
                second = progress.read(path)
                progress.report("measure", 0, 4)
                third = progress.read(path)
        self.assertEqual(first, {"phase": "compile", "phase_started": 100.0, "done": 0, "total": 4, "item": "fib aihc-native-O0"})
        self.assertEqual(second["phase_started"], 100.0)
        self.assertEqual(second["done"], 1)
        self.assertEqual(third["phase_started"], 200.0)

    def test_read_tolerates_a_missing_file(self):
        self.assertIsNone(progress.read(Path("/nonexistent/progress.json")))


class EstimateTests(unittest.TestCase):
    medians = {"compiler_build": 100.0, "compile": 600.0, "measure": 300.0}

    def test_cells_say_how_far_a_phase_is(self):
        state = {"phase": "compile", "phase_started": 1000.0, "done": 3, "total": 6}
        fraction, left = estimate_progress(state, 400.0, self.medians, now=1300.0)
        self.assertAlmostEqual(fraction, (100 + 300) / 1000)
        # Three cells took 300 s, so three more take another 300, then measuring.
        self.assertAlmostEqual(left, 300 + 300)

    def test_a_phase_without_cells_goes_by_its_usual_length(self):
        state = {"phase": "compiler_build", "phase_started": 1000.0}
        fraction, left = estimate_progress(state, 40.0, self.medians, now=1040.0)
        self.assertAlmostEqual(fraction, 0.04)
        self.assertAlmostEqual(left, 60 + 900)
        fraction, left = estimate_progress(state, 500.0, self.medians, now=1500.0)
        self.assertAlmostEqual(fraction, 0.095)
        self.assertAlmostEqual(left, 900)

    def test_without_phases_it_goes_by_the_total(self):
        fraction, left = estimate_progress({"phase": "compile", "done": 1, "total": 2}, 250.0, {"total": 1000.0}, now=0)
        self.assertAlmostEqual(fraction, 0.25)
        self.assertAlmostEqual(left, 750)
        self.assertEqual(estimate_progress(None, 10.0, {}, now=0), (None, None))

    def test_phase_medians(self):
        timings = [{"compile": 10.0, "total": 10.0}, {"compile": 30.0, "measure": 4.0, "total": 34.0}, {"compile": 20.0, "total": 20.0}]
        self.assertEqual(phase_medians(timings), {"compile": 20.0, "measure": 0.0})

    def test_progress_bar(self):
        self.assertEqual(progress_bar(0.5, 10), "[#####-----]")


class DiskTests(unittest.TestCase):
    def test_sizes_per_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cache" / "aihc-stores").mkdir(parents=True)
            (root / ".cache" / "aihc-stores" / "blob").write_bytes(b"x" * 200_000)
            (root / ".cache" / "small").write_bytes(b"x")
            sizes = directory_sizes(root / ".cache")
            self.assertEqual(set(sizes), {"aihc-stores", "small"})
            self.assertGreaterEqual(sizes["aihc-stores"], 200_000)
            (root / ".state").mkdir()
            disk = DiskUsage(root, root / ".state")
            self.assertIn("measuring", disk.lines()[0])
            disk.refresh()
            disk.wait()
            self.assertTrue(disk.lines()[0].startswith("disk       .cache "))
            self.assertIn("aihc-stores", disk.lines()[0])
            self.assertIn("free of", disk.lines()[1])

    def test_format_bytes(self):
        self.assertEqual(format_bytes(312.4e9), "312.4 GB")
        self.assertEqual(format_bytes(1.2e12), "1.2 TB")


if __name__ == "__main__":
    unittest.main()
