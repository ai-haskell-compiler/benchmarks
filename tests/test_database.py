import json
import tempfile
import unittest
from pathlib import Path

from aihc_bench.database import Database


def commit(index, key):
    return {"sha": f"c{index}", "ordinal": index, "committed_at": "2026-01-01", "subject": str(index), "tree_key": key}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "state.sqlite3")
        # c0..c2 share a compiler, c3 differs, c4..c5 share another.
        self.database.replace_commits([commit(0, "A"), commit(1, "A"), commit(2, "A"), commit(3, "B"), commit(4, "C"), commit(5, "C")])

    def tearDown(self):
        self.database.close()
        self.directory.cleanup()

    def record(self, sha, status="complete"):
        self.database.start_attempt("exp", "plat", sha, f"run-{sha}", {"id": "env"}, "machine")
        self.database.finish_attempt("exp", "plat", sha, status, {"aihc_commit": {"sha": sha}, "compiler_status": "available", "results": []})

    def test_results_propagate_to_same_tree_commits(self):
        self.record("c1")
        written = self.database.propagate_inherited("exp", "plat")
        self.assertEqual(written, 2)
        attempts = {attempt["commit_sha"]: attempt for attempt in self.database.terminal_attempts("exp", "plat")}
        self.assertEqual(set(attempts), {"c0", "c1", "c2"})
        self.assertEqual(attempts["c0"]["status"], "inherited")
        self.assertEqual(attempts["c0"]["inherited_from"], "c1")
        self.assertEqual(attempts["c0"]["run_id"], "run-c1")
        envelope = json.loads(attempts["c2"]["result_json"])
        self.assertEqual(envelope["aihc_commit"]["sha"], "c2")
        self.assertEqual(envelope["inherited_from"], "c1")
        self.assertEqual(self.database.propagate_inherited("exp", "plat"), 0)

    def test_nearest_source_wins_and_measurements_are_kept(self):
        self.record("c0")
        self.database.propagate_inherited("exp", "plat")
        self.record("c2")
        self.database.propagate_inherited("exp", "plat")
        attempts = {attempt["commit_sha"]: attempt for attempt in self.database.terminal_attempts("exp", "plat")}
        self.assertEqual(attempts["c2"]["status"], "complete")
        # c1 is equidistant from c0 and c2; ties resolve toward the newer source.
        self.assertEqual(attempts["c1"]["inherited_from"], "c2")

    def test_unavailable_results_propagate_too(self):
        self.record("c4", status="unavailable")
        self.database.propagate_inherited("exp", "plat")
        attempts = {attempt["commit_sha"]: attempt for attempt in self.database.terminal_attempts("exp", "plat")}
        self.assertEqual(attempts["c5"]["status"], "inherited")

    def test_forget_drops_inherited_results(self):
        self.record("c1")
        self.database.propagate_inherited("exp", "plat")
        self.assertTrue(self.database.forget("exp", "plat", "c1"))
        self.assertEqual(self.database.terminal_attempts("exp", "plat"), [])

    def test_replace_commits_drops_commits_outside_the_history(self):
        self.record("c0", status="failed")
        self.record("c4")
        self.database.replace_commits([commit(3, "B"), commit(4, "C"), commit(5, "C")])
        self.assertEqual([item["sha"] for item in self.database.commits()], ["c3", "c4", "c5"])
        attempts = self.database.terminal_attempts("exp", "plat")
        self.assertEqual([attempt["commit_sha"] for attempt in attempts], ["c4"])

    def test_commits_without_tree_keys_never_inherit(self):
        self.database.replace_commits(self.database.commits() + [{"sha": "c6", "ordinal": 6, "committed_at": "2026-01-01", "subject": "6"}])
        self.record("c3")
        self.database.propagate_inherited("exp", "plat")
        self.assertEqual({attempt["commit_sha"] for attempt in self.database.terminal_attempts("exp", "plat")}, {"c3"})


if __name__ == "__main__":
    unittest.main()
