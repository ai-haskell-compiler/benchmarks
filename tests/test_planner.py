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

    def test_first_commit_is_second_even_with_a_large_signal(self):
        plan = build_plan(self.commits, [measured("c4", 100), measured("c8", 1000)])
        self.assertEqual(plan["stage"], "first")
        self.assertEqual(plan["next"]["sha"], "c0")

    def test_gap_bisection(self):
        plan = build_plan(self.commits, [measured("c0"), measured("c8")])
        self.assertEqual(plan["stage"], "signal")
        self.assertEqual(plan["next"]["sha"], "c4")
        self.assertEqual(plan["gaps"][0]["width"], 7)
        self.assertEqual(plan["gaps"][0]["signal"], 0.0)

    def test_signal_prioritizes_the_gap_with_a_change(self):
        attempts = [measured("c0", 200), measured("c4", 100), measured("c8", 100)]
        plan = build_plan(self.commits, attempts)
        self.assertEqual(plan["next"]["sha"], "c2")
        self.assertGreater(plan["gaps"][0]["signal"], 0.6)

    def test_signal_outweighs_width_and_recency(self):
        # A single older commit with a sharp change wins over five newer
        # commits with a small change, regardless of their timestamps.
        attempts = [measured("c0", 200), measured("c2", 100), measured("c8", 101)]
        plan = build_plan(self.commits, attempts)
        self.assertEqual(plan["next"]["sha"], "c1")
        self.assertEqual([gap["width"] for gap in plan["gaps"]], [1, 5])

    def test_width_then_recency_break_signal_ties(self):
        attempts = [measured("c0"), measured("c2"), measured("c8")]
        self.assertEqual(select_next(self.commits, attempts)["sha"], "c5")
        attempts = [measured("c0"), measured("c4"), measured("c8")]
        gaps = rank_gaps(self.commits, {attempt["commit_sha"]: attempt for attempt in attempts})
        self.assertEqual([gap["pick"]["sha"] for gap in gaps], ["c6", "c2"])

    def test_unavailable_endpoints_carry_no_signal(self):
        attempts = [measured("c0", status="unavailable"), measured("c8", 500)]
        plan = build_plan(self.commits, attempts)
        self.assertEqual(plan["gaps"][0]["signal"], 0.0)
        self.assertEqual(plan["next"]["sha"], "c4")

    def test_relative_changes_treat_improvements_and_regressions_equally(self):
        for left, right in [(100, 200), (200, 100)]:
            with self.subTest(left=left):
                attempts = [measured("c0", left), measured("c4", right), measured("c8", right)]
                self.assertEqual(select_next(self.commits, attempts)["sha"], "c2")

    def test_allocations_and_other_benchmarks_can_supply_the_strongest_signal(self):
        left = measured("c0")
        data = json.loads(left["result_json"])
        data["results"].append({
            "benchmark": "ack", "configuration": "aihc-native-O0",
            "measurement": {"metrics": [{"metric": "allocated_bytes", "estimate": 4000}]},
        })
        left["result_json"] = json.dumps(data)
        right = measured("c4")
        data["results"][-1]["measurement"]["metrics"][0]["estimate"] = 1000
        right["result_json"] = json.dumps(data)
        end = dict(right, commit_sha="c8")
        self.assertEqual(select_next(self.commits, [left, right, end])["sha"], "c2")

    def test_repeated_bisection_localizes_a_step_change(self):
        attempts = [measured("c0", 100), measured("c8", 200)]
        order = []
        for _ in range(3):
            pick = select_next(self.commits, attempts)
            order.append(pick["sha"])
            attempts.append(measured(pick["sha"], 100 if pick["ordinal"] < 3 else 200))
        self.assertEqual(order, ["c4", "c2", "c3"])

    def test_equal_values_eventually_cover_the_history(self):
        attempts = []
        order = []
        while (pick := select_next(self.commits, attempts)) is not None:
            order.append(pick["sha"])
            attempts.append(measured(pick["sha"]))
        self.assertEqual(order[:3], ["c8", "c0", "c4"])
        self.assertEqual(len(order), len(self.commits))
        self.assertEqual(len(set(order)), len(self.commits))

    def test_empty_and_single_commit_histories(self):
        self.assertIsNone(select_next([], []))
        self.assertEqual(select_next(self.commits[:1], [])["sha"], "c0")
        self.assertIsNone(select_next(self.commits[:1], [measured("c0")]))

    def test_a_new_head_takes_priority_over_existing_gaps(self):
        attempts = [measured("c0"), measured("c4", 200), measured("c8", 200)]
        new_head = {"sha": "c9", "ordinal": 9}
        self.assertEqual(select_next(self.commits + [new_head], attempts), new_head)

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
        self.assertEqual(plan["next"]["sha"], "c0")
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
