import subprocess
import tempfile
import unittest
from pathlib import Path

from aihc_bench.git_history import commits, tree_keys


def git(root, *arguments):
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin:/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin"},
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


if __name__ == "__main__":
    unittest.main()
