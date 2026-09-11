import subprocess
import tempfile
import unittest
from pathlib import Path

from aihc_bench.git_history import GitError, commits, parse_cutoff, tree_keys


def git(root, *arguments, date=None):
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin:/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin"},
    )


class TreeKeyTests(unittest.TestCase):
    def test_tree_key_changes_only_with_compiler_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-q", "-b", "main")
            (root / "bin").mkdir()
            (root / "bin" / "aihc.txt").write_text("one")
            (root / "README").write_text("docs")
            git(root, "add", "."); git(root, "commit", "-q", "-m", "compiler")
            (root / "README").write_text("docs changed")
            git(root, "commit", "-q", "-am", "docs only")
            (root / "bin" / "aihc.txt").write_text("two")
            git(root, "commit", "-q", "-am", "compiler again")
            (root / "flake.lock").write_text("lock")
            git(root, "add", "."); git(root, "commit", "-q", "-m", "add lock")

            history = commits(root, "main", ["bin", "flake.lock"])
            keys = [commit["tree_key"] for commit in history]
            self.assertEqual(len(history), 4)
            self.assertEqual(keys[0], keys[1])
            self.assertNotEqual(keys[1], keys[2])
            self.assertNotEqual(keys[2], keys[3])
            self.assertEqual(tree_keys(root, [history[0]["sha"]], ["bin", "flake.lock"])[history[0]["sha"]], keys[0])
            self.assertEqual(tree_keys(root, [history[0]["sha"]], [])[history[0]["sha"]], "no-paths")


class CutoffTests(unittest.TestCase):
    def test_parse_cutoff_reads_dates_as_utc_midnight(self):
        self.assertEqual(parse_cutoff("2026-09-01"), 1788220800)
        self.assertEqual(parse_cutoff("2026-09-01T00:00:00Z"), 1788220800)
        self.assertEqual(parse_cutoff("2026-09-01T02:00:00+02:00"), 1788220800)
        with self.assertRaises(GitError):
            parse_cutoff("september")

    def test_since_drops_older_commits_but_keeps_ordinals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-q", "-b", "main")
            (root / "bin").mkdir()
            (root / "bin" / "aihc.txt").write_text("one")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "ancient", date="2026-08-30T12:00:00Z")
            (root / "bin" / "aihc.txt").write_text("two")
            git(root, "commit", "-q", "-am", "eve", date="2026-08-31T23:59:59Z")
            (root / "bin" / "aihc.txt").write_text("three")
            git(root, "commit", "-q", "-am", "first usable", date="2026-09-01T00:00:00Z")
            (root / "bin" / "aihc.txt").write_text("four")
            git(root, "commit", "-q", "-am", "later", date="2026-09-02T00:00:00Z")

            everything = commits(root, "main", ["bin"])
            history = commits(root, "main", ["bin"], since="2026-09-01")
            self.assertEqual([commit["subject"] for commit in history], ["first usable", "later"])
            self.assertEqual([commit["ordinal"] for commit in history], [2, 3])
            self.assertEqual(history, everything[2:])
            with self.assertRaises(GitError):
                commits(root, "main", ["bin"], since="2027-01-01")


if __name__ == "__main__":
    unittest.main()
