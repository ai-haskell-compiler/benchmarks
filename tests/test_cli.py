import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aihc_bench.cli import _refresh_history, _upload_after_commit
from aihc_bench.git_history import GitError
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


class RefreshHistoryTests(unittest.TestCase):
    """A sweep runs for hours and the branch moves while it does.

    Planning once meant a run kept choosing from the history it loaded at
    the start, so a commit that landed an hour in was invisible until the
    whole sweep finished -- and that is usually the commit most worth
    measuring.
    """

    CONFIG = {"aihc_ref": "origin/main", "aihc_tree_paths": ["compiler"], "aihc_since": "2026-09-20"}

    def _call(self, fetch_first=True, fetch_side_effect=None, history=None):
        database = MagicMock()
        history = history if history is not None else [{"sha": "a" * 40, "ordinal": 0}]
        with (
            patch("aihc_bench.cli.fetch", side_effect=fetch_side_effect) as fetch,
            patch("aihc_bench.cli.commits", return_value=history) as commits,
        ):
            returned = _refresh_history(database, self.CONFIG, Path("/repo"), "platform", {"b": "e"}, fetch_first)
        return database, fetch, commits, returned

    def test_the_history_is_fetched_and_reloaded(self):
        database, fetch, commits, returned = self._call()
        fetch.assert_called_once_with(Path("/repo"))
        commits.assert_called_once()
        database.replace_commits.assert_called_once_with(returned)

    def test_without_fetch_the_history_is_still_reloaded(self):
        """A local checkout that someone else updates is still worth rereading."""
        database, fetch, commits, _ = self._call(fetch_first=False)
        fetch.assert_not_called()
        commits.assert_called_once()

    def test_a_failed_fetch_does_not_stop_the_sweep(self):
        """The branch being briefly unreachable is no reason to throw away
        hours of measuring still to come; the cloned history is still good."""
        database, _, commits, returned = self._call(fetch_side_effect=GitError("network unreachable"))
        commits.assert_called_once()
        self.assertEqual(returned, [{"sha": "a" * 40, "ordinal": 0}])

    def test_inherited_results_are_propagated_for_every_experiment(self):
        database = MagicMock()
        with (
            patch("aihc_bench.cli.fetch"),
            patch("aihc_bench.cli.commits", return_value=[]),
        ):
            _refresh_history(database, self.CONFIG, Path("/repo"), "platform", {"b1": "e1", "b2": "e2"}, True)
        self.assertEqual(
            sorted(call.args[0] for call in database.propagate_inherited.call_args_list), ["e1", "e2"]
        )
