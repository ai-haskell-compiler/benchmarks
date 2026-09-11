import unittest
from pathlib import Path

from aihc_bench.measurement import compile_metrics, measure_adaptively, relative_difference
from aihc_bench.process import ProcessMeasurement


class FakeRunner:
    def __init__(self, times, stats=None, allocations=None):
        self.times = iter(times)
        self.stats = stats
        self.allocations = iter(allocations) if allocations else None

    def __call__(self, command, cwd, timeout):
        stats = dict(self.stats) if self.stats else None
        if stats is not None and self.allocations is not None:
            stats["allocated_bytes"] = next(self.allocations)
        return ProcessMeasurement(
            command=list(command),
            wall_time_ns=next(self.times),
            cpu_time_ns=50,
            peak_rss_bytes=8_388_608,
            exit_code=0,
            stdout=b"ok\n",
            stderr=b"",
            timed_out=False,
            runtime_stats=stats,
        )


STATS = {"peak_heap_bytes": 4096, "allocated_bytes": 1_000_000, "gc_count": 3, "gc_time_ns": 700}


def metric(result, name):
    return next(item for item in result["metrics"] if item["metric"] == name)


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
        self.assertEqual(metric(result, "wall_time")["estimate"], 100)
        self.assertEqual(metric(result, "cpu_time")["estimate"], 50)
        self.assertEqual(metric(result, "peak_rss")["estimate"], 8_388_608)

    def test_wall_time_estimate_is_the_median_of_stable_buckets(self):
        result = measure_adaptively(
            ["unused"], Path("."), b"ok\n", 1, 0.6, 64, invoke=FakeRunner([100, 100, 1000, 100, 100, 100, 1000])
        )
        self.assertEqual(result["bucket_sizes"], [1, 2, 4])
        self.assertEqual(metric(result, "wall_time")["estimate"], 100)

    def test_doubles_buckets_and_stops_at_limit(self):
        result = measure_adaptively(
            ["unused"], Path("."), b"ok\n", 1, 0.01, 4,
            invoke=FakeRunner([100, 200, 200, 400, 400, 400, 400]),
        )
        self.assertEqual(result["status"], "nonconverged")
        self.assertEqual(result["bucket_sizes"], [1, 2, 4])
        self.assertEqual(len(result["samples"]), 7)

    def test_runtime_stats_are_unavailable_without_a_hook(self):
        result = measure_adaptively(["unused"], Path("."), b"ok\n", 1, 0.01, 64, invoke=FakeRunner([100, 100, 100]))
        self.assertEqual(metric(result, "peak_heap")["status"], "unavailable")
        self.assertIsNone(metric(result, "allocated_bytes")["estimate"])

    def test_runtime_stats_become_metrics(self):
        result = measure_adaptively(
            ["unused"], Path("."), b"ok\n", 1, 0.01, 64, invoke=FakeRunner([100, 100, 100], stats=STATS)
        )
        self.assertEqual(metric(result, "peak_heap")["estimate"], 4096)
        self.assertEqual(metric(result, "allocated_bytes")["status"], "ok")
        self.assertEqual(metric(result, "gc_count")["estimate"], 3)
        self.assertEqual(metric(result, "gc_time")["estimate"], 700)

    def test_deterministic_metric_disagreement_is_recorded(self):
        result = measure_adaptively(
            ["unused"], Path("."), b"ok\n", 1, 0.01, 64,
            invoke=FakeRunner([100, 100, 100], stats=STATS, allocations=[10, 10, 11]),
        )
        self.assertEqual(metric(result, "allocated_bytes")["status"], "nondeterministic")

    def test_wrong_output_is_validation_failure(self):
        runner = FakeRunner([100])
        original = runner.__call__

        def wrong_output(command, cwd, timeout):
            sample = original(command, cwd, timeout)
            return ProcessMeasurement(**{**sample.__dict__, "stdout": b"wrong\n"})

        result = measure_adaptively(["unused"], Path("."), b"ok\n", 1, 0.01, 2, invoke=wrong_output)
        self.assertEqual(result["status"], "validation_failed")

    def test_compile_metrics_follow_the_compile_result(self):
        metrics = compile_metrics({"status": "compiled", "wall_time_ns": 5, "artifact_bytes": 9})
        self.assertEqual([(item["metric"], item["estimate"]) for item in metrics], [("compile_time", 5), ("artifact_size", 9)])
        cached = compile_metrics({"status": "compiled", "artifact_bytes": 9, "cached": True})
        self.assertEqual(cached[0]["status"], "unavailable")
        self.assertEqual(cached[1]["estimate"], 9)


if __name__ == "__main__":
    unittest.main()
