"""The core package and the GHC core stores; see "Core libraries" in docs/architecture.md."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.config import load_config
from aihc_bench.freeze import parse_freeze
from aihc_bench.runner import Cell, prepare_ghc_core_stores, with_ghc_core_stores

ROOT = Path(__file__).resolve().parent.parent


def _lock_entries(path: Path) -> dict:
    """``(platform, name) -> entry`` for the Hackage packages of an ``aihc.lock``."""
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        (platform, entry["name"]): entry
        for platform, entries in document["platforms"].items()
        for entry in entries
        if entry["source"] == "hackage"
    }


def _identity(entry: dict) -> tuple:
    return entry["version"], entry.get("revision"), entry.get("flags") or {}


class CorePackageTests(unittest.TestCase):
    """A benchmark finds a core library in the store only if its own plan asks
    for exactly that unit; anything else rebuilds it inside the timed compile,
    silently. These hold the pins of the core package and the benchmarks
    together."""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "benchmark.json")
        cls.core = Path(cls.config["_core_dir"])

    def test_the_core_lock_and_freeze_pin_the_same_versions(self):
        core_lock = _lock_entries(self.core / "aihc.lock")
        freeze = parse_freeze(self.core / "cabal.project.freeze")
        for platform in {platform for platform, _ in core_lock}:
            versions = {name: entry["version"] for (where, name), entry in core_lock.items() if where == platform}
            # The lock also carries what the core libraries depend on.
            self.assertEqual({name: versions[name] for name in freeze}, freeze, platform)

    def test_every_core_library_is_a_direct_dependency_of_the_core_package(self):
        cabal = (self.core / "core-libraries.cabal").read_text(encoding="utf-8")
        depends = set(re.findall(r"(?m)^    ([A-Za-z0-9-]+),?$", cabal)) - {"base"}
        self.assertEqual(depends, set(parse_freeze(self.core / "cabal.project.freeze")))

    def test_every_benchmark_lock_agrees_with_the_core_lock(self):
        core_lock = _lock_entries(self.core / "aihc.lock")
        for lock in sorted((ROOT / "benchmarks").glob("*/aihc.lock")):
            for key, entry in _lock_entries(lock).items():
                if key in core_lock:
                    self.assertEqual(_identity(entry), _identity(core_lock[key]), f"{lock.parent.name}: {key}")

    def test_no_lock_repeats_a_key(self):
        """aihc rejects a lock with a key given twice ("an object has the
        same key two times") where Python's json keeps the last one; a
        repeated "linux-x86_64" failed every AIHC cell of sha-digest."""
        def unique(pairs):
            keys = [key for key, _ in pairs]
            self.assertEqual(len(keys), len(set(keys)), keys)
            return dict(pairs)

        for lock in [self.core / "aihc.lock", *sorted((ROOT / "benchmarks").glob("*/aihc.lock"))]:
            with self.subTest(lock=lock.parent.name):
                json.loads(lock.read_text(encoding="utf-8"), object_pairs_hook=unique)

    def test_benchmark_freezes_agree_with_the_core_pins(self):
        """A boot library in a freeze file carries the version that GHC
        shipped; the generated project ignores it. Every other core pin has
        to match, or the benchmark was frozen against a different library."""
        core = parse_freeze(self.core / "cabal.project.freeze")
        boot = set(self.config.get("ghc_boot_libraries", []))
        for freeze in sorted((ROOT / "benchmarks").glob("*/cabal.project.freeze")):
            for name, version in parse_freeze(freeze).items():
                if name in core and name not in boot:
                    self.assertEqual(version, core[name], f"{freeze.parent.name}: {name}")


def _cell(configuration: dict, command=("compile",)) -> Cell:
    return Cell(
        benchmark={"id": "sample"},
        configuration=configuration,
        commit_sha="abc",
        artifact=Path("/a/program"),
        build_dir=Path("/a/build"),
        compile_cwd=Path("/"),
        compile_environment={},
        compile_command=list(command) if command else None,
        run_command=None,
        unavailable_reason=None,
        setup_error=None,
    )


GHC = {"id": "ghc-native-O1", "compiler_family": "ghc", "compile": ["python3", "{root}/compile.py", "--source", "{source}", "--build-dir", "{build_dir}", "--artifact", "{artifact}", "--exe", "{package}"]}
AIHC = {"id": "aihc-native-O1", "compiler_family": "aihc", "compile": ["{aihc}", "build", "{source}"]}


class GhcCoreStoreTests(unittest.TestCase):
    def _config(self, root: Path) -> dict:
        core = root / "core"
        core.mkdir()
        (core / "cabal.project.freeze").write_text("constraints: any.text ==2.1.4\n", encoding="utf-8")
        return {"_core_dir": str(core), "_core_sha256": "c0", "platforms": {"test": {}}}

    def test_a_store_is_built_once_with_the_configurations_own_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            commands = []

            def build(command, cwd, timeout, environment=None):
                commands.append(command)
                store = Path(command[command.index("--store-dir") + 1])
                (store / "ghc-9.14.1" / "package.db").mkdir(parents=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.runner.run_command", side_effect=build):
                first = prepare_ghc_core_stores(config, "test", root, [GHC, AIHC, GHC], 60)
                second = prepare_ghc_core_stores(config, "test", root, [GHC], 60)
            self.assertEqual(len(commands), 1, "the store outlives the commit, and AIHC has its own")
            command = commands[0]
            self.assertEqual(command[command.index("--exe") + 1], "core-libraries")
            self.assertEqual(command[command.index("--source") + 1], config["_core_dir"])
            self.assertEqual(command[command.index("--core-freeze") + 1], str(Path(config["_core_dir"]) / "cabal.project.freeze"))
            self.assertEqual(first, second)
            self.assertTrue(first["ghc-native-O1"].is_file())
            self.assertEqual(first["ghc-native-O1"].name, "store-prepared.tar")

    def test_a_store_that_fails_to_build_leaves_its_cells_as_they_were(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            failed = subprocess.CompletedProcess([], 1, "", "no ghc")
            with patch("aihc_bench.runner.run_command", return_value=failed):
                self.assertEqual(prepare_ghc_core_stores(config, "test", root, [GHC], 60), {})

    def test_without_a_core_package_nothing_is_built(self):
        with patch("aihc_bench.runner.run_command", side_effect=AssertionError("must not build")):
            self.assertEqual(prepare_ghc_core_stores({"platforms": {"test": {}}}, "test", Path("/"), [GHC], 60), {})

    def test_ghc_cells_compile_against_their_configurations_store(self):
        archive = Path("/cache/ghc-native-O1-abc/store-prepared.tar")
        cells = [_cell(GHC), _cell(AIHC), _cell(GHC, command=None), _cell({**GHC, "id": "ghc-llvm-O1"})]
        attached = with_ghc_core_stores(cells, {"ghc-native-O1": archive}, "/core")
        self.assertEqual(
            attached[0].compile_command,
            ["compile", "--store-dir", "/cache/ghc-native-O1-abc/store", "--core-freeze", "/core/cabal.project.freeze"],
        )
        self.assertEqual(attached[0].store_archive, archive)
        # AIHC has its own store; an unavailable cell compiles nothing; a
        # configuration whose store did not build keeps its private one.
        self.assertEqual(attached[1:], cells[1:])


if __name__ == "__main__":
    unittest.main()
