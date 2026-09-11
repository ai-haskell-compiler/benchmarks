import gzip
import json
import tempfile
import unittest
from pathlib import Path

from aihc_bench.database import Database
from aihc_bench.uploader import UploadError, load_credentials, register, save_credentials, upload_pending


class FakeServer:
    def __init__(self):
        self.requests = []
        self.known = set()

    def __call__(self, url, body, headers):
        payload = json.loads(gzip.decompress(body) if headers.get("Content-Encoding") == "gzip" else body)
        self.requests.append((url, payload, headers))
        if headers.get("Authorization") != "Bearer machine-token":
            return 403, {"error": "unknown token"}
        if url.endswith("/api/commits"):
            return 200, {"upserted": len(payload["commits"])}
        if url.endswith("/api/upload"):
            run_id = payload["run_id"] + ("~" + payload["aihc_commit"]["sha"][:12] if payload.get("inherited_from") else "")
            if run_id in self.known:
                return 200, {"run_id": run_id, "inserted": False}
            self.known.add(run_id)
            return 201, {"run_id": run_id, "inserted": True}
        return 404, {"error": "nope"}


class UploaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        self.database = Database(self.state / "state.sqlite3")
        self.database.replace_commits([
            {"sha": "c0" * 20, "ordinal": 0, "committed_at": "2026-01-01", "subject": "0", "tree_key": "A"},
            {"sha": "c1" * 20, "ordinal": 1, "committed_at": "2026-01-01", "subject": "1", "tree_key": "A"},
        ])
        self.database.start_attempt("exp", "plat", "c0" * 20, "run-0", {"id": "env"}, "machine")
        self.database.finish_attempt("exp", "plat", "c0" * 20, "complete", {"schema_version": 2, "run_id": "run-0", "aihc_commit": {"sha": "c0" * 20}, "compiler_status": "available", "results": []})
        self.database.propagate_inherited("exp", "plat")
        self.credentials = {"server": "https://fast.test", "token": "machine-token"}

    def tearDown(self):
        self.database.close()
        self.directory.cleanup()

    def test_credentials_round_trip(self):
        self.assertIsNone(load_credentials(self.state))
        path = save_credentials(self.state, "https://fast.test/", "tok")
        self.assertEqual(load_credentials(self.state), {"server": "https://fast.test", "token": "tok"})
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_register_stores_token_from_server(self):
        def post(url, body, headers):
            self.assertEqual(headers["Authorization"], "Bearer admin")
            return 201, {"machine_id": json.loads(body)["machine_id"], "token": "issued"}

        self.assertEqual(register("https://fast.test", "admin", "apple-m4-abc123", "laptop", post=post), "issued")
        with self.assertRaises(UploadError):
            register("https://fast.test", "admin", "apple-m4-abc123", None, post=lambda *_: (403, {"error": "no"}))

    def test_uploads_sources_before_inherited_and_marks_acknowledged(self):
        server = FakeServer()
        summary = upload_pending(self.database, "exp", "plat", self.credentials, self.database.commits(), post=server, log=lambda _: None)
        self.assertEqual(summary, {"commits": 2, "uploaded": 2, "skipped": 0, "pending": 0})
        urls = [url for url, _, _ in server.requests]
        self.assertEqual(urls, ["https://fast.test/api/commits", "https://fast.test/api/upload", "https://fast.test/api/upload"])
        self.assertIsNone(server.requests[1][1].get("inherited_from"))
        self.assertEqual(server.requests[2][1]["inherited_from"], "c0" * 20)
        self.assertEqual(server.requests[1][2]["Content-Encoding"], "gzip")
        self.assertTrue(server.requests[1][2]["User-Agent"].startswith("aihc-bench/"))
        self.assertEqual(self.database.pending_uploads("exp", "plat"), [])

        again = upload_pending(self.database, "exp", "plat", self.credentials, self.database.commits(), post=server, log=lambda _: None)
        self.assertEqual(again["uploaded"], 0)

    def test_dry_run_uploads_nothing(self):
        server = FakeServer()
        summary = upload_pending(self.database, "exp", "plat", self.credentials, self.database.commits(), dry_run=True, post=server, log=lambda _: None)
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(server.requests, [])

    def test_failed_upload_stops_and_keeps_pending(self):
        with self.assertRaises(UploadError):
            upload_pending(self.database, "exp", "plat", {"server": "https://fast.test", "token": "wrong"}, self.database.commits(), post=FakeServer(), log=lambda _: None)
        self.assertEqual(len(self.database.pending_uploads("exp", "plat")), 2)

    def test_rerun_resets_upload_state(self):
        server = FakeServer()
        upload_pending(self.database, "exp", "plat", self.credentials, self.database.commits(), post=server, log=lambda _: None)
        self.database.start_attempt("exp", "plat", "c0" * 20, "run-0b", {"id": "env"}, "machine")
        self.database.finish_attempt("exp", "plat", "c0" * 20, "complete", {"schema_version": 2, "run_id": "run-0b", "aihc_commit": {"sha": "c0" * 20}, "compiler_status": "available", "results": []})
        self.assertEqual([a["commit_sha"] for a in self.database.pending_uploads("exp", "plat")], ["c0" * 20])


if __name__ == "__main__":
    unittest.main()
