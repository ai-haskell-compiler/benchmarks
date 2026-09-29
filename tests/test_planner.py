import json
import unittest
from datetime import datetime, timedelta, timezone

from aihc_bench.planner import build_plan, commit_bucket, merge_terminal_attempts, rank_gaps, select_next

#: Days before HEAD for each commit of a history spanning every bucket:
#: three older than half a year, three in the half year, three in the month,
#: three in the week and three in the day.
AGES = [400, 300, 200, 150, 100, 50, 25, 20, 10, 6, 4, 2, 0.5, 0.25, 0]


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

    def test_gap_bisection_within_a_bucket(self):
        attempts = [measured("c8"), measured("c7"), measured("c6")]
        plan = build_plan(self.commits, attempts)
        self.assertEqual(plan["stage"], "day")
        self.assertEqual(plan["next"]["sha"], "c3")
        self.assertEqual(plan["gaps"][0]["width"], 6)
        self.assertEqual(plan["gaps"][0]["signal"], 0.0)

    def test_signal_prioritizes_the_gap_with_a_change(self):
        attempts = [measured("c0", 100), measured("c4", 100), measured("c8", 200)]
        with_signal = build_plan(self.commits, attempts)
        self.assertEqual(with_signal["next"]["sha"], "c6")
        self.assertGreater(with_signal["gaps"][0]["signal"], 0.6)
        without = build_plan(self.commits, [measured("c0"), measured("c4"), measured("c8")])
        self.assertEqual(without["next"]["sha"], "c6")
        self.assertEqual(without["gaps"][0]["signal"], 0.0)
        older = build_plan(self.commits, [measured("c0", 200), measured("c4", 100), measured("c8", 100)])
        self.assertEqual(older["next"]["sha"], "c2")

    def test_recency_breaks_ties_between_equal_gaps(self):
        attempts = [measured("c0"), measured("c4"), measured("c8")]
        gaps = rank_gaps(self.commits, {attempt["commit_sha"]: attempt for attempt in attempts})
        self.assertEqual([gap["pick"]["sha"] for gap in gaps], ["c6", "c2"])
        self.assertGreater(gaps[0]["recency"], 0)

    def test_unavailable_endpoints_carry_no_signal(self):
        attempts = [measured("c0", status="unavailable"), measured("c8", 500)]
        plan = build_plan(self.commits, attempts)
        self.assertEqual(plan["gaps"][0]["signal"], 0.0)
        self.assertEqual(plan["next"]["sha"], "c4")

    def aged_history(self):
        head = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        return [
            {"sha": f"a{index}", "ordinal": index, "committed_at": (head - timedelta(days=age)).isoformat(), "subject": str(index)}
            for index, age in enumerate(AGES)
        ]

    def test_commits_fall_in_buckets_by_age_before_head(self):
        history = self.aged_history()
        names = [commit_bucket(commit, history[-1]) for commit in history]
        self.assertEqual(names, ["older"] * 3 + ["half-year"] * 3 + ["month"] * 3 + ["week"] * 3 + ["day"] * 3)
        # A zone offset is honoured, and a commit dated after HEAD is the newest.
        self.assertEqual(commit_bucket({"committed_at": "2026-09-29T20:00:00+10:00"}, history[-1]), "day")
        self.assertEqual(commit_bucket({"committed_at": "2026-09-30T00:00:00+00:00"}, history[-1]), "day")

    def test_buckets_fill_evenly_newest_first(self):
        history = self.aged_history()
        attempts = []
        order = []
        while True:
            plan = build_plan(history, attempts)
            if plan["next"] is None:
                break
            order.append((plan["next"]["sha"], plan["stage"]))
            attempts.append(measured(plan["next"]["sha"]))
        self.assertEqual(order[0], ("a14", "head"))
        # HEAD fills the day bucket's first slot, then every other bucket
        # catches up, newest first, before any gets its second.
        self.assertEqual([stage for _, stage in order[1:5]], ["week", "month", "half-year", "older"])
        self.assertEqual([stage for _, stage in order[5:10]], ["day", "week", "month", "half-year", "older"])
        self.assertEqual(len(order), len(history))

    def test_a_bucket_that_is_full_drops_out(self):
        history = self.aged_history()
        # The day and week buckets are fully measured; the rest have one each.
        attempts = [measured(f"a{index}") for index in (0, 3, 6, 9, 10, 11, 12, 13, 14)]
        plan = build_plan(history, attempts)
        self.assertEqual(plan["stage"], "month")
        self.assertEqual(plan["next"]["sha"], "a8")
        self.assertEqual([(bucket["name"], bucket["measured"], bucket["size"]) for bucket in plan["buckets"]], [
            ("day", 3, 3), ("week", 3, 3), ("month", 1, 3), ("half-year", 1, 3), ("older", 1, 3),
        ])

    def test_gaps_are_clipped_to_the_chosen_bucket(self):
        history = self.aged_history()
        # One gap runs from a1 to a13, across four buckets.
        attempts = [measured("a0"), measured("a14", 100), measured("a13", 100), measured("a12", 100)]
        plan = build_plan(history, attempts)
        self.assertEqual(plan["stage"], "week")
        self.assertEqual([gap["width"] for gap in plan["gaps"]], [3])
        self.assertEqual(plan["next"]["sha"], "a10")

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
        plan = build_plan(self.commits, merged.values())
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
