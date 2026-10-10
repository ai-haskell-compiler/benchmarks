import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from aihc_bench.database import Database
from aihc_bench.planner import build_plan
from aihc_bench.schedule import Window
from aihc_bench.watch import (
    RunBusy,
    explain,
    finish_time,
    format_duration,
    lock_holder,
    other_runs,
    recent_commit_seconds,
    remaining_commits,
    run_lock,
)


def commit(index, tree_key=None):
    return {
        "sha": f"{index:040x}",
        "ordinal": index,
        "committed_at": f"2026-10-0{index % 9 + 1}T12:00:00Z",
        "subject": f"commit {index}",
        "tree_key": tree_key or f"tree{index}",
    }


def measured(sha, wall):
    return {
        "commit_sha": sha,
        "status": "complete",
        "result": {
            "compiler_status": "available",
            "results": [{
                "benchmark": "fib", "configuration": "aihc-native-O2",
                "measurement": {"metrics": [{"metric": "wall_time", "estimate": wall}]},
            }],
        },
    }


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.commits = [commit(index) for index in range(9)]

    def test_head(self):
        lines = explain(build_plan(self.commits, []))
        self.assertEqual(lines, [f"commit {self.commits[8]['sha'][:12]} from 2026-10-09 is next because it is HEAD, the newest commit on origin/main, and has no result yet"])

    def test_tail(self):
        lines = explain(build_plan(self.commits, [measured(self.commits[8]["sha"], 100)]), since="2026-10-01")
        self.assertIn("because it is TAIL, the oldest commit in the benchmark window (since 2026-10-01)", lines[0])

    def test_signal_names_the_difference_and_where_it_was_seen(self):
        attempts = [measured(self.commits[0]["sha"], 110e6), measured(self.commits[4]["sha"], 100e6), measured(self.commits[8]["sha"], 100e6)]
        lines = explain(build_plan(self.commits, attempts))
        self.assertIn(f"commit {self.commits[2]['sha'][:12]} from 2026-10-03 is next because of a 9.1% wall time difference between its neighbours", lines[0])
        self.assertEqual(lines[1], f"fib aihc-native-O2: 110.0 ms -> 100.0 ms; it splits the 3 unmeasured commits between {self.commits[0]['sha'][:12]} and {self.commits[4]['sha'][:12]}")

    def test_no_signal_bisects_the_widest_gap(self):
        attempts = [measured(self.commits[0]["sha"], 100), measured(self.commits[8]["sha"], 100)]
        self.assertIn("it splits the 7 unmeasured commits", explain(build_plan(self.commits, attempts))[0])

    def test_caught_up(self):
        attempts = [measured(c["sha"], 100) for c in self.commits]
        self.assertEqual(explain(build_plan(self.commits, attempts)), ["nothing to benchmark: every commit has a result"])


class EstimateTests(unittest.TestCase):
    def test_remaining_counts_each_tree_once(self):
        commits = [commit(0, "a"), commit(1, "a"), commit(2, "b"), commit(3, "b"), commit(4, "c")]
        self.assertEqual(remaining_commits(commits, [{"commit_sha": commits[0]["sha"]}]), 2)

    def test_finish_time_waits_for_windows(self):
        windows = [Window.parse("22:00-06:00")]
        start = datetime(2026, 10, 10, 12, 0)
        # Two four-hour commits: 22:00-02:00, then 02:00-06:00.
        self.assertEqual(finish_time(start, 2, 4 * 3600, windows), datetime(2026, 10, 11, 6, 0))
        # A third cannot start at 06:00 and waits for the next night.
        self.assertEqual(finish_time(start, 3, 4 * 3600, windows), datetime(2026, 10, 12, 2, 0))
        self.assertEqual(finish_time(start, 3, 3600, []), datetime(2026, 10, 10, 15, 0))

    def test_format_duration(self):
        self.assertEqual(format_duration(12 * 60), "12m")
        self.assertEqual(format_duration(107 * 60), "1h47m")
        self.assertEqual(format_duration(55 * 3600), "2d 7h")

    def test_recent_commit_seconds_uses_uploaded_measurements_once_per_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.sqlite3")
            database.replace_commits([commit(index) for index in range(4)])

            def attempt(experiment, index, total, uploaded=True, inherited=None):
                sha = commit(index)["sha"]
                database.start_attempt(experiment, "p", sha, f"run{index}", {})
                database.finish_attempt(experiment, "p", sha, "complete", {"timing": {"total_ns": total * 1e9}})
                database.connection.execute(
                    "UPDATE attempts SET uploaded_at=?, inherited_from=?, finished_at=? WHERE experiment_id=? AND commit_sha=?",
                    ("now" if uploaded else None, inherited, f"2026-10-0{index + 1}", experiment, sha),
                )

            attempt("e1", 0, 100)
            attempt("e2", 0, 100)
            attempt("e1", 1, 200)
            attempt("e1", 2, 300, uploaded=False)
            attempt("e1", 3, 400, inherited="x")
            database.connection.commit()
            self.assertEqual(recent_commit_seconds(database), [200, 100])
            database.close()


class RunDetectionTests(unittest.TestCase):
    def test_the_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.lock"
            self.assertIsNone(lock_holder(path))
            with run_lock(path):
                self.assertIsNotNone(lock_holder(path))
                with self.assertRaises(RunBusy):
                    with run_lock(path):
                        pass
            self.assertIsNone(lock_holder(path))

    def test_other_runs(self):
        listing = "\n".join([
            "101 python3 -m aihc_bench run --fetch --upload",
            "102 python3 -m aihc_bench watch",
            "103 /nix/store/abc-aihc-bench/bin/aihc-bench run",
            "104 grep aihc_bench run",
            "105 nix run . -- run --fetch",
        ])
        self.assertEqual(other_runs(listing), ["101", "103"])


if __name__ == "__main__":
    unittest.main()
