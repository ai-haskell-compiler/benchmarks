import unittest

from aihc_bench.planner import select_next


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.commits = [
            {"sha": f"c{index}", "ordinal": index, "committed_at": "2026-01-01", "subject": str(index)}
            for index in range(9)
        ]

    def test_head_is_always_first(self):
        self.assertEqual(select_next(self.commits, [])["sha"], "c8")

    def test_root_follows_head(self):
        self.assertEqual(select_next(self.commits, [{"commit_sha": "c8"}])["sha"], "c0")

    def test_maximally_spaced_midpoint_follows_endpoints(self):
        attempts = [{"commit_sha": "c0"}, {"commit_sha": "c8"}]
        self.assertEqual(select_next(self.commits, attempts)["sha"], "c4")

    def test_terminal_failures_count_as_measured(self):
        attempts = [{"commit_sha": commit["sha"], "status": "unavailable"} for commit in self.commits]
        self.assertIsNone(select_next(self.commits, attempts))


if __name__ == "__main__":
    unittest.main()
