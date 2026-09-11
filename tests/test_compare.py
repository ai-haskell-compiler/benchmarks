import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.compare import (
    CompareError,
    Side,
    bootstrap_ratio,
    format_report,
    measure_interleaved,
    run_compare,
    select_configuration,
    summarize,
)
from aihc_bench.database import Database
from aihc_bench.process import ProcessMeasurement
from aihc_bench.runner import build_cells


def configuration(identifier, family="aihc", profile="O2", backend="native"):
    return {
        "id": identifier,
        "compiler_family": family,
        "compiler_version": "commit" if family == "aihc" else "9.14.1",
        "backend": backend,
        "gc": "semispace",
        "optimization": profile,
        "aihc_target": "{aihc_native_target}",
        "runtime_stats": "aihc",
        "compile": ["compiler", "{source}", "--output", "{artifact}"],
        "run": ["{artifact}"],
    }


CONFIG = {
    "platforms": {"test-platform": {"aihc_native_target": "test-native"}},
    "measurement": {"process_timeout_seconds": 5, "compile_timeout_seconds": 5, "relative_threshold": 0.01, "maximum_bucket_size": 4},
    "benchmarks": [
        {"id": "fib", "source": "fib.hs", "expected_stdout": "ok\n"},
        {"id": "fact", "source": "fact.hs", "expected_stdout": "ok\n"},
    ],
    "configurations": [
        configuration("aihc-native-O2"),
        configuration("aihc-native-O0", profile="O0"),
        configuration("ghc-native-O2", family="ghc"),
    ],
}
CAPABILITIES = {"build-exe": True, "compile": False, "prepare-runtime": False, "install-offline": False, "optimization-flag": True, "optimization-O1": True, "optimization-Os": True, "build-root": False}


def sample(wall, stats=None):
    return ProcessMeasurement(command=[], wall_time_ns=wall, cpu_time_ns=wall - 5, peak_rss_bytes=1000, exit_code=0, stdout=b"ok\n", stderr=b"", timed_out=False, runtime_stats=stats)


class CompareTests(unittest.TestCase):
    def test_selection_defaults_to_aihc_and_filters(self):
        selected = select_configuration(CONFIG)
        self.assertEqual([item["id"] for item in selected["configurations"]], ["aihc-native-O2", "aihc-native-O0"])
        selected = select_configuration(CONFIG, benchmarks=["fib"], configurations=["ghc-native-O2"], profile="O2")
        self.assertEqual([item["id"] for item in selected["benchmarks"]], ["fib"])
        self.assertEqual([item["id"] for item in selected["configurations"]], ["ghc-native-O2"])
        with self.assertRaises(CompareError):
            select_configuration(CONFIG, benchmarks=["nope"])
        with self.assertRaises(CompareError):
            select_configuration(CONFIG, configurations=["ghc-native-O2"], profile="O0")

    def test_summary_reports_medians_ratio_and_interval(self):
        a = [{"wall_time_ns": value, "cpu_time_ns": value, "peak_rss_bytes": 10, "allocated_bytes": 100} for value in (100, 102, 98, 101, 99)]
        b = [{"wall_time_ns": value, "cpu_time_ns": value, "peak_rss_bytes": 10, "allocated_bytes": 90} for value in (80, 82, 78, 81, 79)]
        metrics = {metric["metric"]: metric for metric in summarize(a, b)}
        self.assertEqual(metrics["wall_time"]["a"], 100)
        self.assertEqual(metrics["wall_time"]["b"], 80)
        self.assertAlmostEqual(metrics["wall_time"]["ratio"], 0.8)
        self.assertTrue(metrics["wall_time"]["significant"])
        self.assertLess(metrics["wall_time"]["ci"][1], 1.0)
        self.assertAlmostEqual(metrics["allocated_bytes"]["ratio"], 0.9)
        self.assertIsNone(metrics["peak_heap"]["ratio"])
        self.assertFalse(summarize(a, a)[0]["significant"])

    def test_bootstrap_is_deterministic_and_bounded(self):
        first = bootstrap_ratio([10, 11, 12], [10, 11, 12], seed=1)
        second = bootstrap_ratio([10, 11, 12], [10, 11, 12], seed=1)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], 1.0)
        self.assertGreaterEqual(first[1], 1.0)

    def test_measurement_interleaves_sides_and_records_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("fib.hs", "fact.hs"):
                (root / name).write_text("main = putStrLn \"ok\"\n")
            selected = select_configuration(CONFIG, profile="O2")
            with patch.dict(os.environ, {"AIHC_BENCH_TOOLCHAINS": "/toolchains"}):
                sides = []
                for label in ("aaaa", "bbbb"):
                    cells = build_cells(selected, "test-platform", {"sha": label * 10}, root / label, root, {b["id"]: "compare" for b in selected["benchmarks"]}, capabilities=CAPABILITIES)
                    sides.append([(cell, {"status": "compiled", "artifact_bytes": 1}) for cell in cells])
            sides[1][1] = (sides[1][1][0], {"status": "compile_failed", "stderr": "boom"})

            calls = []

            def invoke(command, cwd, timeout, environment_overrides=None, stats_file=None, stats_format=None):
                calls.append(command[0])
                wall = 100 if "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in command[0] else 90
                return sample(wall, {"peak_heap_bytes": 5, "allocated_bytes": 7, "gc_count": 1, "gc_time_ns": 2})

            results = measure_interleaved((sides[0], sides[1]), selected, root, rounds=3, invoke=invoke, log=lambda _: None)

        by_key = {(entry["benchmark"], entry["configuration"]): entry for entry in results}
        fib = by_key[("fib", "aihc-native-O2")]
        self.assertEqual([side["status"] for side in fib["sides"]], ["ok", "ok"])
        self.assertEqual(len(fib["sides"][0]["samples"]), 3)
        wall = next(metric for metric in fib["metrics"] if metric["metric"] == "wall_time")
        self.assertAlmostEqual(wall["ratio"], 0.9)
        heap = next(metric for metric in fib["metrics"] if metric["metric"] == "peak_heap")
        self.assertEqual(heap["a"], 5)
        fact = by_key[("fact", "aihc-native-O2")]
        self.assertEqual(fact["sides"][1]["status"], "compile_failed")
        self.assertEqual(fact["sides"][0]["samples"], [])
        # one warm-up round plus three measured rounds, two sides each, one runnable cell
        self.assertEqual(len(calls), 8)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(calls[0], calls[2])

    def test_run_compare_cleans_up_and_records_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fib.hs").write_text("main = putStrLn \"ok\"\n")
            selected = select_configuration(CONFIG, benchmarks=["fib"], profile="O2")
            sides = (Side("old", "a" * 40, root / "wt-a", True), Side("new", "b" * 40, root / "wt-b", True))
            removed = []

            def fake_compile(cells, root_, timeout, jobs):
                return [(cell, {"status": "compiled", "artifact_bytes": 1}) for cell in cells]

            with (
                patch.dict(os.environ, {"AIHC_BENCH_TOOLCHAINS": "/toolchains"}),
                patch("aihc_bench.compare.create_worktree"),
                patch("aihc_bench.compare.remove_worktree", side_effect=lambda repo, path: removed.append(path)),
                patch("aihc_bench.compare.probe_capabilities", return_value=(CAPABILITIES, None)),
                patch("aihc_bench.compare.compile_cells", side_effect=fake_compile),
            ):
                report = run_compare(
                    config=selected, platform_id="test-platform", root=root, aihc_repository=root, sides=sides, rounds=2, jobs=1,
                    invoke=lambda *args, **kwargs: sample(100), log=lambda _: None,
                )
            self.assertEqual(removed, [root / "wt-a", root / "wt-b"])
            self.assertEqual([side["label"] for side in report["sides"]], ["old", "new"])
            database = Database(root / "state.sqlite3")
            database.record_adhoc(report)
            self.assertEqual(database.adhoc_runs()[0]["a_label"], "old")
            self.assertEqual(database.pending_uploads("exp", "plat"), [])
            database.close()

            text = format_report(report)
            self.assertIn("A = old (aaaaaaaaaaaa), B = new (bbbbbbbbbbbb)", text)
            self.assertIn("wall_time", text)
            markdown = format_report(report, markdown=True)
            self.assertIn("| fib | `aihc-native-O2` | wall_time |", markdown)


if __name__ == "__main__":
    unittest.main()
