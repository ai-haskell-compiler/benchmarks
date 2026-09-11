import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aihc_bench.database import Database
from aihc_bench.uploader import (
    UploadError,
    check_login,
    commit_statement,
    envelope_key,
    refresh_overview,
    run_row_id,
    run_statements,
    sql_literal,
    upload_pending,
)

CONFIG = {"publishing": {"wrangler_config": "web/wrangler.jsonc", "bucket": "bucket", "database": "db"}}


def envelope(sha, run_id="run-0", inherited_from=None):
    record = {
        "schema_version": 2,
        "run_id": run_id,
        "created_at": "2026-09-10T00:00:00Z",
        "experiment_id": "exp",
        "machine_id": "apple-m4-abc123",
        "environment": {"id": "env-1", "cpu_brand": "Apple M4"},
        "aihc_commit": {"sha": sha, "ordinal": 3, "committed_at": "2026-09-01", "subject": "it's fast", "tree_key": "k"},
        "compiler_status": "available",
        "unavailable_reason": None,
        "results": [
            {
                "benchmark": "fib",
                "configuration": "aihc-native-semispace-O2",
                "compiler_family": "aihc",
                "compiler_version": sha,
                "backend": "native",
                "optimization": "O2",
                "baseline": False,
                "measurement": {"status": "converged", "metrics": [
                    {"metric": "wall_time", "unit": "ns", "status": "ok", "estimate": 90, "samples": [90, 91]},
                    {"metric": "peak_heap", "unit": "byte", "status": "unavailable", "estimate": None, "samples": []},
                ]},
            },
            {
                "benchmark": "fib",
                "configuration": "aihc-native-semispace-O0",
                "compiler_family": "aihc",
                "compiler_version": sha,
                "backend": "native",
                "optimization": "O0",
                "measurement": {"status": "unavailable", "reason": "missing_capability:optimization-flag"},
            },
        ],
    }
    if inherited_from:
        record["inherited_from"] = inherited_from
    return record


class FakeWrangler:
    """Records wrangler invocations and captures the SQL files before they are deleted."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.sql = []
        self.fail_on = fail_on

    def __call__(self, command):
        self.calls.append(command)
        if "--file" in command and command[1] != "r2":
            self.sql.append(Path(command[command.index("--file") + 1]).read_text())
        if self.fail_on and self.fail_on in command:
            return subprocess.CompletedProcess(command, 1, "", "boom")
        stdout = "👋 You are logged in with an OAuth Token, associated with the email x@y.\n" if "whoami" in command else "[]"
        return subprocess.CompletedProcess(command, 0, stdout, "")


class UploaderTests(unittest.TestCase):
    def test_sql_literals_escape_quotes_and_types(self):
        self.assertEqual(sql_literal("it's"), "'it''s'")
        self.assertEqual(sql_literal(None), "NULL")
        self.assertEqual(sql_literal(True), "1")
        self.assertEqual(sql_literal(3), "3")

    def test_run_statements_cover_every_table(self):
        statements = run_statements(envelope("a" * 40), "raw/v2/m/a/run-0.json.gz")
        text = "\n".join(statements)
        self.assertIn("INSERT INTO machines", text)
        self.assertIn("INSERT INTO commits", text)
        self.assertIn("INSERT OR IGNORE INTO environments", text)
        self.assertIn("INSERT OR IGNORE INTO runs", text)
        self.assertEqual(text.count("INSERT OR REPLACE INTO measurements"), 2)
        self.assertIn("'it''s fast'", text)
        self.assertIn("'peak_heap', 'byte', 'unavailable', NULL, 0", text)
        self.assertNotIn("aihc-native-semispace-O0", text)

    def test_inherited_runs_reuse_the_source_envelope(self):
        source = envelope("a" * 40)
        inherited = envelope("b" * 40, inherited_from="a" * 40)
        self.assertEqual(envelope_key(source), f"raw/v2/apple-m4-abc123/{'a' * 40}/run-0.json.gz")
        self.assertEqual(envelope_key(inherited), envelope_key(source))
        self.assertEqual(run_row_id(inherited), f"run-0~{'b' * 12}")
        self.assertEqual(run_row_id(source), "run-0")

    def test_commit_statement_upserts(self):
        statement = commit_statement({"sha": "c", "ordinal": 1, "committed_at": "t", "subject": "s", "tree_key": None})
        self.assertIn("ON CONFLICT(sha) DO UPDATE", statement)
        self.assertIn("NULL)", statement)

    def test_check_login_requires_a_session(self):
        self.assertIn("logged in", check_login(CONFIG, Path("/root"), run=FakeWrangler()))
        with self.assertRaises(UploadError):
            check_login(CONFIG, Path("/root"), run=lambda command: subprocess.CompletedProcess(command, 1, "", "not authenticated"))

    def test_refresh_overview_pings_the_worker_and_tolerates_failure(self):
        config = {"publishing": {**CONFIG["publishing"], "server_url": "https://perf.example/"}}
        calls = []
        self.assertTrue(refresh_overview(config, opener=lambda url, timeout: calls.append(url), log=lambda _: None))
        self.assertEqual(calls, ["https://perf.example/api/overview?refresh=1"])

        def failing(url, timeout):
            raise OSError("offline")

        messages = []
        self.assertFalse(refresh_overview(config, opener=failing, log=messages.append))
        self.assertIn("offline", messages[0])

    def test_upload_uses_wrangler_and_marks_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.sqlite3")
            database.replace_commits([
                {"sha": "a" * 40, "ordinal": 0, "committed_at": "t", "subject": "0", "tree_key": "A"},
                {"sha": "b" * 40, "ordinal": 1, "committed_at": "t", "subject": "1", "tree_key": "A"},
            ])
            database.start_attempt("exp", "plat", "a" * 40, "run-0", {"id": "env-1"}, "apple-m4-abc123")
            database.finish_attempt("exp", "plat", "a" * 40, "complete", envelope("a" * 40))
            database.propagate_inherited("exp", "plat")
            wrangler = FakeWrangler()
            summary = upload_pending(database, "exp", "plat", CONFIG, Path("/root"), database.commits(), run=wrangler, log=lambda _: None)
            self.assertEqual(summary, {"commits": 2, "uploaded": 2, "pending": 0})
            kinds = [(call[1] if call[0] == "wrangler" and call[1] != "--config" else call[3]) for call in wrangler.calls]
            self.assertEqual(kinds, ["d1", "r2", "d1", "d1"])
            put = wrangler.calls[1]
            self.assertEqual(put[4], f"bucket/raw/v2/apple-m4-abc123/{'a' * 40}/run-0.json.gz")
            self.assertIn("--content-encoding", put)
            self.assertIn("--remote", put)
            self.assertIn("INSERT INTO commits", wrangler.sql[0])
            self.assertIn(f"'run-0~{'b' * 12}'", wrangler.sql[2])
            self.assertEqual(database.pending_uploads("exp", "plat"), [])

            again = upload_pending(database, "exp", "plat", CONFIG, Path("/root"), database.commits(), run=wrangler, log=lambda _: None)
            self.assertEqual(again["uploaded"], 0)
            database.close()

    def test_failed_wrangler_call_keeps_the_run_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.sqlite3")
            database.replace_commits([{"sha": "a" * 40, "ordinal": 0, "committed_at": "t", "subject": "0", "tree_key": "A"}])
            database.start_attempt("exp", "plat", "a" * 40, "run-0", {"id": "env-1"}, "apple-m4-abc123")
            database.finish_attempt("exp", "plat", "a" * 40, "complete", envelope("a" * 40))
            with self.assertRaises(UploadError):
                upload_pending(database, "exp", "plat", CONFIG, Path("/root"), database.commits(), run=FakeWrangler(fail_on="put"), log=lambda _: None)
            self.assertEqual(len(database.pending_uploads("exp", "plat")), 1)
            dry = upload_pending(database, "exp", "plat", CONFIG, Path("/root"), database.commits(), dry_run=True, run=FakeWrangler(fail_on="d1"), log=lambda _: None)
            self.assertEqual(dry["pending"], 1)
            database.close()


if __name__ == "__main__":
    unittest.main()
