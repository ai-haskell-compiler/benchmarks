import json
import unittest

from aihc_bench.planner import build_plan, merge_terminal_attempts, rank_gaps, select_next


def envelope(wall):
    return json.dumps({
        "compiler_status": "available",
        "results": [{
            "benchmark": "fib", "configuration": "aihc-native-O2",
            "measurement": {"metrics": [{"metric": "wall_time", "estimate": wall}, {"metric": "allocated_bytes", "estimate": 1000}]},
        }],
    })


def measured(sha, wall=100, status="complete"):
    return {"commit_sha": sha, "status": status, "result_json": envelope(wall) if status == "complete" else None}


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.commits = [
            {"sha": f"c{index}", "ordinal": index, "committed_at": "2026-01-01", "subject": str(index)}
            for index in range(9)
        ]

    def test_head_is_always_first(self):
        self.assertEqual(select_next(self.commits, [])["sha"], "c8")

    def test_warmup_fills_recent_commits_newest_first(self):
        attempts = [measured("c8")]
        plan = build_plan(self.commits, attempts, warmup=3)
        self.assertEqual(plan["next"]["sha"], "c7")
        self.assertEqual(plan["stage"], "warmup")
        plan = build_plan(self.commits, attempts + [measured("c7")], warmup=3)
        self.assertEqual(plan["next"]["sha"], "c6")

    def test_gap_bisection_after_warmup(self):
        attempts = [measured("c8"), measured("c7"), measured("c6")]
        plan = build_plan(self.commits, attempts, warmup=3)
        self.assertEqual(plan["stage"], "gap")
        self.assertEqual(plan["next"]["sha"], "c3")
        self.assertEqual(plan["gaps"][0]["width"], 6)
        self.assertEqual(plan["gaps"][0]["signal"], 0.0)

    def test_signal_prioritizes_the_gap_with_a_change(self):
        attempts = [measured("c0", 100), measured("c4", 100), measured("c8", 200)]
        with_signal = build_plan(self.commits, attempts, warmup=1)
        self.assertEqual(with_signal["next"]["sha"], "c6")
        self.assertGreater(with_signal["gaps"][0]["signal"], 0.6)
        without = build_plan(self.commits, [measured("c0"), measured("c4"), measured("c8")], warmup=1)
        self.assertEqual(without["next"]["sha"], "c6")
        self.assertEqual(without["gaps"][0]["signal"], 0.0)
        older = build_plan(self.commits, [measured("c0", 200), measured("c4", 100), measured("c8", 100)], warmup=1)
        self.assertEqual(older["next"]["sha"], "c2")

    def test_recency_breaks_ties_between_equal_gaps(self):
        attempts = [measured("c0"), measured("c4"), measured("c8")]
        gaps = rank_gaps(self.commits, {attempt["commit_sha"]: attempt for attempt in attempts})
        self.assertEqual([gap["pick"]["sha"] for gap in gaps], ["c6", "c2"])
        self.assertGreater(gaps[0]["recency"], 0)

    def test_unavailable_endpoints_carry_no_signal(self):
        attempts = [measured("c0", status="unavailable"), measured("c8", 500)]
        plan = build_plan(self.commits, attempts, warmup=1)
        self.assertEqual(plan["gaps"][0]["signal"], 0.0)
        self.assertEqual(plan["next"]["sha"], "c4")

    def test_inherited_results_count_as_measured(self):
        attempts = [measured(f"c{index}") for index in range(9)]
        attempts[3]["status"] = "inherited"
        self.assertIsNone(select_next(self.commits, attempts))

    def test_merge_requires_every_experiment(self):
        by_experiment = {
            "fib-1": [measured("c8", 100), measured("c4", 100), {**measured("c2"), "inherited_from": "c4"}],
            "ack-2": [measured("c8", 300), {**measured("c2"), "inherited_from": "c4"}],
        }
        merged = {attempt["commit_sha"]: attempt for attempt in merge_terminal_attempts(by_experiment)}
        # c4 is only measured for fib, so it stays unmeasured for planning.
        self.assertEqual(set(merged), {"c8", "c2"})
        self.assertEqual(merged["c8"]["status"], "complete")
        self.assertIsNone(merged["c8"]["inherited_from"])
        self.assertEqual(len(merged["c8"]["result"]["results"]), 2)
        self.assertEqual(merged["c2"]["status"], "inherited")
        self.assertEqual(merged["c2"]["inherited_from"], "c4")
        plan = build_plan(self.commits, merged.values(), warmup=1)
        self.assertEqual(plan["next"]["sha"], "c5")
        self.assertEqual(merge_terminal_attempts({}), [])

    def test_merge_marks_unavailable_only_when_no_benchmark_built(self):
        by_experiment = {"a": [measured("c1", status="unavailable")], "b": [measured("c1", status="unavailable")]}
        merged = merge_terminal_attempts(by_experiment)
        self.assertEqual(merged[0]["status"], "unavailable")
        self.assertEqual(merged[0]["result"]["compiler_status"], "unavailable")
        mixed = merge_terminal_attempts({"a": [measured("c1", status="unavailable")], "b": [measured("c1")]})
        self.assertEqual(mixed[0]["status"], "complete")
        self.assertEqual(mixed[0]["result"]["compiler_status"], "available")

    def test_terminal_failures_count_as_measured(self):
        attempts = [{"commit_sha": commit["sha"], "status": "unavailable"} for commit in self.commits]
        self.assertIsNone(select_next(self.commits, attempts))


if __name__ == "__main__":
    unittest.main()
