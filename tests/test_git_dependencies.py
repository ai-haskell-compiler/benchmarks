import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.config import ConfigError, experiment_ids, load_config
from aihc_bench.git_dependencies import GitDependencyError, stage_git_dependencies, validate_git_dependencies
from aihc_bench.runner import build_cells, compile_cells
from tests.test_config import BASE, write_config
from tests.test_runner import aihc_configuration


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True, stderr=subprocess.PIPE).strip()


class GitDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "upstream"
        self.repository.mkdir()
        git(self.repository, "init", "--quiet")
        for name in ("aihc-tc", "aihc-resolve", "aihc-parser"):
            package = self.repository / "components" / name
            package.mkdir(parents=True)
            (package / f"{name}.cabal").write_text(f"name: {name}\nversion: 0.1.0.0\n")
            (package / "Lib.hs").write_text("old source")
        self.first = self.commit()
        (self.repository / "components" / "aihc-tc" / "Lib.hs").write_text("new source")
        self.second = self.commit()
        self.dependencies = [{
            "url": str(self.repository), "commit": self.first,
            "packages": {"aihc-tc": "components/aihc-tc", "aihc-resolve": "components/aihc-resolve"},
        }]
        self.cache = self.root / "cache"
        self.destination = self.root / "staged"

    def commit(self):
        git(self.repository, "add", ".")
        git(self.repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")
        return git(self.repository, "rev-parse", "HEAD")

    def stage(self):
        stage_git_dependencies(self.dependencies, self.cache, self.destination, 10)
        return self.destination / "workspace"

    def test_pinned_sources_are_fresh_and_cache_works_offline(self):
        workspace = self.stage()
        self.assertEqual((workspace / "aihc-tc" / "Lib.hs").read_text(), "old source")
        self.assertEqual(sorted(p.name for p in workspace.iterdir()), ["aihc-resolve", "aihc-tc"])
        # The repository layout is intact, but parser is not a resolver candidate.
        self.assertTrue((workspace / "aihc-tc" / ".." / "aihc-parser").is_dir())
        (workspace / "aihc-tc" / "Lib.hs").write_text("modified by build")
        (workspace / "aihc-tc" / "build.o").touch()
        shutil.rmtree(self.repository)
        with patch("aihc_bench.git_dependencies._git", side_effect=AssertionError("cache must not access Git")):
            workspace = self.stage()
        self.assertEqual((workspace / "aihc-tc" / "Lib.hs").read_text(), "old source")
        self.assertFalse((workspace / "aihc-tc" / "build.o").exists())

    def test_changing_commit_selects_new_source(self):
        self.stage()
        self.dependencies[0]["commit"] = self.second
        self.assertEqual((self.stage() / "aihc-tc" / "Lib.hs").read_text(), "new source")

    def test_validation_rejects_mutable_refs_duplicate_packages_and_escaping_paths(self):
        original = self.dependencies[0]
        for broken in (
            None, {}, [None], [{**original, "commit": "main"}],
            [{**original, "commit": self.first[:12]}],
            [{**original, "packages": {"aihc-tc": "../outside"}}],
            [{**original, "packages": {"aihc-tc": "/outside"}}],
            [{**original, "packages": {"../bad": "."}}],
            [original, original],
        ):
            with self.subTest(broken=broken), self.assertRaises(GitDependencyError):
                validate_git_dependencies(broken)

    def test_missing_package_or_wrong_name_fails_preparation(self):
        for subdir in ("absent", "components/aihc-resolve"):
            self.dependencies[0]["packages"] = {"aihc-tc": subdir}
            with self.subTest(subdir=subdir), self.assertRaises(GitDependencyError):
                self.stage()

    def test_failed_fetch_does_not_publish_a_cache_entry(self):
        self.dependencies[0]["commit"] = "0" * 40
        with self.assertRaises(GitDependencyError):
            self.stage()
        self.assertEqual(list(self.cache.iterdir()), [])
        self.dependencies[0]["commit"] = self.first
        self.stage()

    def test_pins_change_experiment_identity_and_invalid_pins_fail_config_loading(self):
        path = write_config(self.root, BASE)
        document = json.loads(path.read_text())
        document["benchmarks"][0]["git_dependencies"] = self.dependencies
        path.write_text(json.dumps(document))
        first = experiment_ids(load_config(path))
        self.dependencies[0]["commit"] = self.second
        path.write_text(json.dumps(document))
        self.assertNotEqual(first, experiment_ids(load_config(path)))
        self.dependencies[0]["commit"] = "main"
        path.write_text(json.dumps(document))
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_both_compilers_get_the_selected_packages_before_the_timer(self):
        config = load_config(write_config(self.root, BASE))
        config["benchmarks"][0]["git_dependencies"] = self.dependencies
        config["platforms"] = {"test": {"aihc_native_target": "test-native"}}
        config["configurations"] = [aihc_configuration("native"), BASE]
        cells = build_cells(config, "test", {"sha": "compiler"}, self.root, self.root, {"sample": "experiment"})
        aihc, ghc = cells
        self.assertIn("--workspace", aihc.compile_command)
        self.assertEqual(ghc.compile_command.count("--local-package"), 2)
        self.assertNotIn("aihc-parser", " ".join(ghc.compile_command))
        events = []

        def stage(*args):
            stage_git_dependencies(*args)
            events.append("staged")

        def clock():
            self.assertIn("staged", events)
            return 123

        def compile(command, *args):
            self.assertTrue((ghc.artifact.parent / "dependencies/workspace/aihc-tc/Lib.hs").is_file())
            return subprocess.CompletedProcess(command, 1, "", "expected fixture compile failure")

        with patch("aihc_bench.runner.stage_git_dependencies", side_effect=stage), \
                patch("aihc_bench.runner.time.perf_counter_ns", side_effect=clock), \
                patch("aihc_bench.runner.run_command", side_effect=compile):
            (_, result), = compile_cells([ghc], self.root, 10)
        self.assertEqual(result["status"], "compile_failed")
        self.assertIn("wall_time_ns", result)

    def test_source_preparation_failure_is_reported_without_a_compile_measurement(self):
        config = load_config(write_config(self.root, BASE))
        config["benchmarks"][0]["git_dependencies"] = self.dependencies
        cells = build_cells(config, "aarch64-darwin", {"sha": "compiler"}, self.root, self.root, {"sample": "experiment"})
        self.dependencies[0]["packages"]["aihc-tc"] = "missing"
        with patch("aihc_bench.runner.run_command", side_effect=AssertionError("must not compile")):
            (_, result), = compile_cells(cells, self.root, 10)
        self.assertEqual(result["status"], "compile_failed")
        self.assertIn("Git dependency aihc-tc", result["stderr"])
        self.assertNotIn("wall_time_ns", result)
