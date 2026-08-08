import unittest
from pathlib import Path

from aihc_bench.measurement import measure_adaptively, relative_difference
from aihc_bench.process import ProcessMeasurement


class FakeRunner:
    def __init__(self, times):
        self.times = iter(times)

    def __call__(self, command, cwd, timeout):
        return ProcessMeasurement(
            command=list(command),
            wall_time_ns=next(self.times),
            peak_rss_bytes=8_388_608,
            exit_code=0,
            stdout=b"ok\n",
            stderr=b"",
            timed_out=False,
        )


class MeasurementTests(unittest.TestCase):
    def test_symmetric_relative_difference(self):
        self.assertAlmostEqual(relative_difference(99, 101), 0.02)

    def test_converges_after_initial_three_runs(self):
        result = measure_adaptively(
            ["unused"], Path("."), b"ok\n", 1, 0.01, 64, invoke=FakeRunner([100, 100, 100])
        )
        self.assertEqual(result["status"], "converged")
        self.assertEqual(result["bucket_sizes"], [1, 2])
        self.assertEqual(len(result["samples"]), 3)
        self.assertEqual(result["metrics"][0]["estimate"], 100)

    def test_doubles_buckets_and_stops_at_limit(self):
        result = measure_adaptively(
            ["unused"], Path("."), b"ok\n", 1, 0.01, 4,
            invoke=FakeRunner([100, 200, 200, 400, 400, 400, 400]),
        )
        self.assertEqual(result["status"], "nonconverged")
        self.assertEqual(result["bucket_sizes"], [1, 2, 4])
        self.assertEqual(len(result["samples"]), 7)

    def test_wrong_output_is_validation_failure(self):
        runner = FakeRunner([100])
        original = runner.__call__

        def wrong_output(command, cwd, timeout):
            sample = original(command, cwd, timeout)
            return ProcessMeasurement(**{**sample.__dict__, "stdout": b"wrong\n"})

        result = measure_adaptively(["unused"], Path("."), b"ok\n", 1, 0.01, 2, invoke=wrong_output)
        self.assertEqual(result["status"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
