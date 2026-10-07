import calendar
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.cabal_index import index_state_reached, newest_freeze_index_state, refresh_package_list, stale_package_list


def _config(root: Path, index_state: str):
    source = root / "benchmarks" / "example"
    source.mkdir(parents=True)
    (source / "cabal.project.freeze").write_text(f"constraints: any.text ==2.1.3\nindex-state: hackage.haskell.org {index_state}\n", encoding="utf-8")
    return {"benchmarks": [{"id": "example-v1", "source": "benchmarks/example"}]}


class IndexStateTests(unittest.TestCase):
    def test_the_head_marker_means_the_tarball_dates_the_list(self):
        """cabal writes the word HEAD into 01-index.timestamp after a plain
        `cabal update`. Giving up on it reported the list as fine on three
        workers whose package lists were weeks behind a benchmark's pin."""
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "01-index.tar"
            index.write_bytes(b"tar")
            (Path(directory) / "01-index.timestamp").write_text("HEAD\n", encoding="utf-8")
            old = calendar.timegm(time.strptime("2026-09-19T21:12:00Z", "%Y-%m-%dT%H:%M:%SZ"))
            os.utime(index, (old, old))
            self.assertEqual(index_state_reached(index), "2026-09-19T21:12:00Z")

    def test_an_explicit_marker_is_the_index_state(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "01-index.tar"
            index.write_bytes(b"tar")
            (Path(directory) / "01-index.timestamp").write_text("1791054150\n", encoding="utf-8")
            self.assertEqual(index_state_reached(index), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1791054150)))

    def test_the_newest_pin_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, "2026-10-03T14:53:23Z")
            self.assertEqual(newest_freeze_index_state(config, root), ("2026-10-03T14:53:23Z", "example-v1"))


class RefreshTests(unittest.TestCase):
    def test_a_list_behind_a_pin_is_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, "2026-10-03T14:53:23Z")
            index = root / "01-index.tar"
            index.write_bytes(b"tar")
            (root / "01-index.timestamp").write_text("HEAD", encoding="utf-8")
            old = time.time() - 30 * 24 * 60 * 60
            os.utime(index, (old, old))
            commands = []

            def run(command, cwd, timeout, *rest):
                commands.append(list(command))
                if command == ["cabal", "update"]:
                    now = time.time()
                    os.utime(index, (now, now))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.cabal_index.package_list", return_value=index), patch("aihc_bench.cabal_index.run_command", side_effect=run):
                self.assertIsNone(refresh_package_list(config, root, 30))
            self.assertEqual(commands, [["cabal", "update"]])

    def test_a_list_that_reaches_the_pin_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, "2026-10-03T14:53:23Z")
            index = root / "01-index.tar"
            index.write_bytes(b"tar")
            with patch("aihc_bench.cabal_index.package_list", return_value=index), patch("aihc_bench.cabal_index.run_command") as run:
                self.assertIsNone(stale_package_list(config, root))
                self.assertIsNone(refresh_package_list(config, root, 30))
            run.assert_not_called()

    def test_a_failed_update_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root, "2026-10-03T14:53:23Z")
            index = root / "01-index.tar"
            index.write_bytes(b"tar")
            old = time.time() - 30 * 24 * 60 * 60
            os.utime(index, (old, old))
            with (
                patch("aihc_bench.cabal_index.package_list", return_value=index),
                patch("aihc_bench.cabal_index.run_command", return_value=subprocess.CompletedProcess([], 1, "", "no network")),
            ):
                error = refresh_package_list(config, root, 30)
            self.assertIn("cabal update exited 1", error)
            self.assertIn("no network", error)


if __name__ == "__main__":
    unittest.main()
