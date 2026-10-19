"""Keep the frontend benchmarks' Git/Hackage boundary reproducible."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from aihc_bench.config import load_config
from aihc_bench.freeze import parse_freeze

ROOT = Path(__file__).resolve().parents[1]


class FrontendBenchmarkTests(unittest.TestCase):
    def test_pins_and_locks_use_git_libraries_and_hackage_parser(self):
        config = load_config(ROOT / "benchmark.json")
        commits = set()
        for library in ("resolve", "tc"):
            name = f"aihc-{library}-bench"
            benchmark = next(entry for entry in config["benchmarks"] if entry["package"] == name)
            dependencies, = benchmark["git_dependencies"]
            commits.add(dependencies["commit"])
            self.assertEqual(benchmark["corpus_env"], "AIHC_BENCH_FRONTEND_CORPUS")
            self.assertIn("corpus/frontend", benchmark["corpus_sources"])
            self.assertIn(dependencies["commit"], (ROOT / "corpus/frontend/corpus.nix").read_text())
            expected = {"aihc-resolve": "components/aihc-resolve"}
            if library == "tc":
                expected["aihc-tc"] = "components/aihc-tc"
            self.assertEqual(dependencies["packages"], expected)
            source = ROOT / benchmark["source"]
            cabal = (source / f"{name}.cabal").read_text()
            parser_version = parse_freeze(source / "cabal.project.freeze")["aihc-parser"]
            self.assertIn(f"aihc-parser =={parser_version}", cabal)
            lock = json.loads((source / "aihc.lock").read_text())
            self.assertEqual(set(lock["platforms"]), {"osx-aarch64", "linux-x86_64", "wasi-wasm32"})
            for platform, entries in lock["platforms"].items():
                with self.subTest(benchmark=name, platform=platform):
                    by_name = {entry["name"]: entry for entry in entries}
                    self.assertEqual(by_name["aihc-parser"]["source"], "hackage")
                    self.assertEqual(by_name["aihc-parser"]["version"], parser_version)
                    for package in expected:
                        self.assertEqual(by_name[package]["source"], "local")
                        self.assertIn(f"{package} =={by_name[package]['version']}", cabal)
            self.assertEqual(benchmark["expected_stdout"], "ok\n")
            self.assertTrue(benchmark["profile_argument"])
        self.assertEqual(len(commits), 1)

    def test_scc_scheduler_handles_cycles_diamonds_and_shuffled_input(self):
        resolver = ROOT / "benchmarks/aihc-resolve-bench"
        checker = ROOT / "benchmarks/aihc-tc-bench"
        # Each benchmark remains self-contained for scratch copies and hashing.
        for name in ("ModuleGraph.hs", "Corpus.hs", "test/GraphTests.hs"):
            self.assertEqual((resolver / name).read_bytes(), (checker / name).read_bytes())
        ghc = shutil.which("ghc")
        if not ghc:
            self.skipTest("GHC unavailable; graph-tests can also be run with cabal test")
        # Use bytecode here: CI may expose a host GHC inside the Nix shell,
        # whose linker cannot find host GMP. Cabal also runs these as native
        # test executables when building the benchmark packages.
        result = subprocess.run(
            [ghc, "-v0", "-ignore-dot-ghci", "-package-env", "-", "-hide-all-packages",
             "-package", "base", "-package", "containers",
             f"-i{resolver}", str(resolver / "test/GraphTests.hs"), "-e", "main"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "SCC scheduling checks passed\n")

    def test_each_profile_and_backend_selects_its_own_package_set(self):
        workloads = json.loads((ROOT / "corpus/frontend/workloads.json").read_text())
        for phase, backends in workloads.items():
            self.assertEqual(set(backends), {"native", "llvm", "wasm"})
            for backend, profiles in backends.items():
                self.assertEqual(set(profiles), {"O0", "Os", "O1", "O2"})
                self.assertEqual(len({tuple(cell["packages"]) for cell in profiles.values()}), 4)
                for cell in profiles.values():
                    self.assertIn("aihc-base", cell["packages"])
                    self.assertGreater(cell["iterations"], 0)
                    self.assertRegex(cell["expected"], r"^packages=\d+ modules=\d+ units=\d+ cyclic=\d+ (resolutions|bindings)=\d+ fnv1a=\d+$")
            for profile in ("O0", "Os", "O1", "O2"):
                selections = {tuple(profiles[profile]["packages"]) for profiles in backends.values()}
                self.assertEqual(len(selections), 3)
