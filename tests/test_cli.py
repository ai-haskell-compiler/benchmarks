import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aihc_bench.cli import _upload_after_commit
from aihc_bench.uploader import UploadError


class UploadAfterCommitTests(unittest.TestCase):
    """A failed upload must not end a run.

    Three long runs died on a transient
    "wrangler d1 execute failed: Authentication error [code: 10000]" that
    succeeded again minutes later, throwing away every commit still to be
    measured. The measurements are already recorded and stay pending.
    """

    def _call(self):
        database = MagicMock()
        database.commits.return_value = []
        _upload_after_commit(database, {"b": "e"}, "suite", "platform", {}, Path("/root"))

    def test_a_failed_upload_is_reported_and_survived(self):
        with (
            patch("aihc_bench.cli.upload_pending", side_effect=UploadError("Authentication error [code: 10000]\nmore")),
            patch("aihc_bench.cli.refresh_overview") as refresh,
            patch("builtins.print") as printed,
        ):
            self._call()
        refresh.assert_not_called()
        message = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("warning", message)
        self.assertIn("retried", message)
        # Only the first line of a multi-line wrangler error is worth printing.
        self.assertNotIn("more", message)

    def test_a_successful_upload_refreshes_the_overview(self):
        with (
            patch("aihc_bench.cli.upload_pending", return_value={"uploaded": 3, "pending": 0}),
            patch("aihc_bench.cli.refresh_overview") as refresh,
            patch("builtins.print"),
        ):
            self._call()
        refresh.assert_called_once()

    def test_an_upload_that_sends_nothing_leaves_the_overview_alone(self):
        with (
            patch("aihc_bench.cli.upload_pending", return_value={"uploaded": 0, "pending": 0}),
            patch("aihc_bench.cli.refresh_overview") as refresh,
            patch("builtins.print"),
        ):
            self._call()
        refresh.assert_not_called()

    def test_a_failing_overview_refresh_is_also_survived(self):
        with (
            patch("aihc_bench.cli.upload_pending", return_value={"uploaded": 3, "pending": 0}),
            patch("aihc_bench.cli.refresh_overview", side_effect=UploadError("edge purge failed")),
            patch("builtins.print") as printed,
        ):
            self._call()
        message = " ".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("warning", message)


if __name__ == "__main__":
    unittest.main()
