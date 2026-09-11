import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.config import load_config
from aihc_bench.runner import (
    _boot_equivalent_dependencies,
    _configured_aihc_targets,
    _prepare_aihc_store,
    build_cells,
    capabilities_from_help,
    compile_cells,
    measure_cells,
    optimization_levels,
    probe_capabilities,
    strip_command,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

NEW_HELP = """aihc - command-line interface for the aihc compiler

Available commands:
  build-exe                Build one Haskell executable
  install                  Build and install one Cabal library
  prepare-runtime          Compile and install target entry and runtime archives
"""
OLD_HELP = "Available commands:\n  compile   Compile a module\n"
# optparse-applicative wraps long help lines, as the real aihc output does.
BUILD_EXE_HELP = """Usage: aihc build-exe FILE --output FILE --target TARGET [--gc semispace] [-O LEVEL]

Available options:
  --target TARGET          Target: apple-arm64, linux-amd64, llvm, or wasm32-wasip3
  -O LEVEL                 Optimization level for C sources and LLVM output: 0,
                           1, 2 or s (default: 2)
  --build-root DIR         Build directory
"""
OLD_BUILD_EXE_HELP = "Usage: aihc build-exe [-O LEVEL]\n  -O LEVEL   Optimization level for C sources and LLVM output: 0 or 2 (default: 2)\n"


def aihc_configuration(backend, profile="O2", **extra):
    entry = {
        "id": f"aihc-{backend}-{profile}",
        "compiler_family": "aihc",
        "compiler_version": "commit",
        "backend": backend,
        "gc": "semispace",
        "optimization": profile,
        "aihc_target": {"native": "{aihc_native_target}", "wasm": "wasm32-wasip3", "llvm": "llvm"}[backend],
        "artifact_suffix": ".wasm" if backend == "wasm" else "",
        "runtime_stats": "aihc",
        "requires": {"O0": ["optimization-flag"], "O1": ["optimization-O1"], "Os": ["optimization-Os"]}.get(profile, []),
        "compile": ["aihc", "{aihc_build_command}", "{source}", "--output", "{artifact}"] + ([f"-{profile}"] if profile != "O2" else []),
        "run": ["wasmtime", "--env", "AIHC_RTS_STATS={stats_file}", "{artifact}"] if backend == "wasm" else ["{artifact}"],
    }
    entry.update(extra)
    return entry


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "platforms": {"test-platform": {"aihc_native_target": "test-native"}},
            "benchmarks": [{"id": "example", "source": "example.hs", "expected_stdout": "ok\n"}],
            "configurations": [
                aihc_configuration("native"),
                aihc_configuration("wasm", compile_path_env="AIHC_BENCH_WASM_CLANG"),
                aihc_configuration("llvm"),
                aihc_configuration("native", "O0"),
                aihc_configuration("native", "O1"),
                aihc_configuration("native", "Os"),
                {
                    "id": "ghc-native-O2",
                    "compiler_family": "ghc",
                    "compiler_version": "9.14.1",
                    "backend": "native",
                    "gc": "ghc-rts",
                    "optimization": "O2",
                    "runtime_stats": "ghc",
                    "compile": ["{toolchains}/bin/ghc-9.14.1", "{source}", "-o", "{artifact}"],
                    "run": ["{artifact}", "+RTS", "-t{stats_file}", "--machine-readable", "-RTS"],
                },
            ],
        }
        self.capabilities = {"build-exe": True, "compile": False, "prepare-runtime": True, "install-offline": False, "optimization-flag": False, "optimization-O1": False, "optimization-Os": False, "build-root": False}

    def build(self, root, capabilities=None, store=None):
        (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
        with patch.dict(os.environ, {"AIHC_BENCH_TOOLCHAINS": "/toolchains"}):
            return self._build(root, capabilities, store)

    def _build(self, root, capabilities, store):
        return build_cells(
            self.config,
            "test-platform",
            {"sha": "abc123"},
            root / "worktree",
            root,
            {"example": "example-experiment"},
            aihc_store=store,
            capabilities=capabilities or self.capabilities,
        )

    def test_reads_capabilities_from_help_text(self):
        new = capabilities_from_help(NEW_HELP)
        self.assertTrue(new["build-exe"] and new["prepare-runtime"])
        self.assertFalse(new["compile"])
        old = capabilities_from_help(OLD_HELP)
        self.assertTrue(old["compile"])
        self.assertFalse(old["build-exe"] or old["prepare-runtime"])

    def test_probe_reads_sub_command_help(self):
        responses = {
            ("--help",): subprocess.CompletedProcess([], 0, NEW_HELP, ""),
            ("build-exe", "--help"): subprocess.CompletedProcess([], 0, BUILD_EXE_HELP, ""),
            ("install", "--help"): subprocess.CompletedProcess([], 0, "Usage: aihc install [--offline] --target T", ""),
        }

        def fake_run(command, cwd, timeout, environment=None):
            return responses[tuple(command[4:])]

        with patch("aihc_bench.runner.run_command", side_effect=fake_run):
            capabilities, error = probe_capabilities(Path("/wt"), Path("/root"), 30)
        self.assertIsNone(error)
        self.assertTrue(capabilities["optimization-flag"])
        self.assertTrue(capabilities["optimization-O1"])
        self.assertTrue(capabilities["optimization-Os"])
        self.assertTrue(capabilities["install-offline"])
        self.assertTrue(capabilities["build-root"])

        responses[("build-exe", "--help")] = subprocess.CompletedProcess([], 0, OLD_BUILD_EXE_HELP, "")
        with patch("aihc_bench.runner.run_command", side_effect=fake_run):
            capabilities, _ = probe_capabilities(Path("/wt"), Path("/root"), 30)
        self.assertTrue(capabilities["optimization-flag"])
        self.assertFalse(capabilities["optimization-O1"] or capabilities["optimization-Os"])

        with patch("aihc_bench.runner.run_command", return_value=subprocess.CompletedProcess([], 1, "", "boom")):
            capabilities, error = probe_capabilities(Path("/wt"), Path("/root"), 30)
        self.assertEqual(error, "boom")
        self.assertFalse(any(capabilities.values()))

    def test_optimization_levels_from_wrapped_help(self):
        self.assertEqual(optimization_levels(BUILD_EXE_HELP), {"0", "1", "2", "s"})
        self.assertEqual(optimization_levels(OLD_BUILD_EXE_HELP), {"0", "2"})
        self.assertEqual(optimization_levels("Usage: aihc build-exe [-O LEVEL] --target T"), set())
        self.assertEqual(optimization_levels(""), set())

    def test_cells_cover_only_the_requested_benchmarks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
            config = {**self.config, "benchmarks": self.config["benchmarks"] + [{"id": "other", "source": "example.hs", "expected_stdout": "ok\n"}]}
            cells = build_cells(config, "test-platform", {"sha": "abc123"}, root / "worktree", root, {"other": "other-exp"}, capabilities=self.capabilities)
            self.assertEqual({cell.benchmark["id"] for cell in cells}, {"other"})
            # Artifacts and stats are cached under the benchmark's own experiment.
            self.assertIn("other-exp", str(cells[0].artifact))
            self.assertEqual(build_cells(config, "test-platform", {"sha": "abc123"}, root / "worktree", root, {}, capabilities=self.capabilities), [])

    def test_build_command_follows_the_commit_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            new = {cell.configuration["id"]: cell for cell in self.build(root)}
            self.assertEqual(new["aihc-native-O2"].compile_command[1], "build-exe")
            old = {cell.configuration["id"]: cell for cell in self.build(root, {**self.capabilities, "build-exe": False, "compile": True})}
            self.assertEqual(old["aihc-native-O2"].compile_command[1], "compile")

    def test_missing_capability_makes_the_configuration_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = {cell.configuration["id"]: cell for cell in self.build(root)}
            self.assertIsNone(cells["aihc-native-O0"].compile_command)
            self.assertEqual(cells["aihc-native-O0"].unavailable_reason, "missing_capability:optimization-flag")
            self.assertEqual(cells["aihc-native-O1"].unavailable_reason, "missing_capability:optimization-O1")
            self.assertEqual(cells["aihc-native-Os"].unavailable_reason, "missing_capability:optimization-Os")
            enabled = {cell.configuration["id"]: cell for cell in self.build(root, {**self.capabilities, "optimization-flag": True, "optimization-O1": True, "optimization-Os": True})}
            self.assertIn("-O0", enabled["aihc-native-O0"].compile_command)
            self.assertIn("-O1", enabled["aihc-native-O1"].compile_command)
            self.assertIn("-Os", enabled["aihc-native-Os"].compile_command)
            # Without a probe result the profile capabilities are absent, so
            # the flag is never sent to a compiler that might ignore it.
            default = {cell.configuration["id"]: cell for cell in self.build(root, {})}
            self.assertIsNone(default["aihc-native-Os"].compile_command)
            self.assertIsNotNone(default["aihc-native-O2"].compile_command)

    def test_build_root_is_per_cell_when_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = {cell.configuration["id"]: cell for cell in self.build(root, {**self.capabilities, "build-root": True})}
            native = cells["aihc-native-O2"]
            self.assertEqual(native.compile_command[-2:], ["--build-root", str(native.build_dir)])
            self.assertNotIn("--build-root", cells["ghc-native-O2"].compile_command)
            without = {cell.configuration["id"]: cell for cell in self.build(root)}
            self.assertNotIn("--build-root", without["aihc-native-O2"].compile_command)

    def test_stats_plumbing_per_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = {cell.configuration["id"]: cell for cell in self.build(root)}
            native = cells["aihc-native-O2"]
            self.assertEqual(native.run_environment, {"AIHC_RTS_STATS": native.stats_file})
            self.assertEqual(native.stats_format, "aihc")
            wasm = cells["aihc-wasm-O2"]
            self.assertIn(f"AIHC_RTS_STATS={wasm.stats_file}", wasm.run_command)
            ghc = cells["ghc-native-O2"]
            self.assertEqual(ghc.compile_command[0], "/toolchains/bin/ghc-9.14.1")
            self.assertEqual(ghc.run_environment, {})
            self.assertIn(f"-t{ghc.stats_file}", ghc.run_command)
            self.assertEqual(ghc.stats_format, "ghc")

    def test_expands_configured_aihc_targets(self):
        with patch.dict(os.environ, {"AIHC_BENCH_WASM_CLANG": "/toolchain/bin"}):
            targets = _configured_aihc_targets(self.config, "test-platform")

        self.assertEqual([target for target, _, _ in targets], ["test-native", "wasm32-wasip3", "llvm"])
        self.assertEqual(targets[1][2]["AIHC_WASM_CLANG"], "/toolchain/bin/clang")

    def test_store_is_added_only_to_aihc_compile_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "store"
            cells = self.build(root, store=store)

        for cell in cells:
            command = cell.compile_command or []
            if cell.configuration["compiler_family"] == "aihc" and command:
                self.assertIn("--store", command)
                self.assertEqual(command[command.index("--store") + 1], str(store))
            else:
                self.assertNotIn("--store", command)

    def test_prepares_runtimes_and_installs_libraries(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            store = root / "store"
            with (
                patch.dict(os.environ, {"AIHC_BENCH_WASM_CLANG": "/toolchain/bin"}),
                patch("aihc_bench.runner.run_command", return_value=completed) as run,
            ):
                errors = _prepare_aihc_store(self.config, "test-platform", worktree, root, store, 30, self.capabilities)
                self.assertEqual(errors, {})
                self.assertNotIn("--offline", run.call_args_list[3].args[0])
                run.reset_mock()
                _prepare_aihc_store(self.config, "test-platform", worktree, root, store, 30, {**self.capabilities, "install-offline": True})

        self.assertEqual(run.call_count, 6)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            [(command[command.index("--target") + 1], command[command.index("--gc") + 1]) for command in commands[:3]],
            [("test-native", "semispace"), ("wasm32-wasip3", "semispace"), ("llvm", "semispace")],
        )
        for command, target in zip(commands[3:], ["test-native", "wasm32-wasip3", "llvm"]):
            self.assertIn("--offline", command)
            self.assertEqual(command[command.index("--target") + 1], target)
        self.assertEqual(run.call_args_list[1].args[3]["AIHC_WASM_CLANG"], "/toolchain/bin/clang")

    def test_failed_wasm_preparation_only_affects_wasm_cells(self):
        def fake_run(command, cwd, timeout, environment=None):
            if "prepare-runtime" in command and "wasm32-wasip3" in command:
                return subprocess.CompletedProcess(command, 1, "", "no sysroot")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("aihc_bench.runner.run_command", side_effect=fake_run):
                errors = _prepare_aihc_store(self.config, "test-platform", root / "worktree", root, root / "store", 30, self.capabilities)
            self.assertEqual(list(errors), ["wasm32-wasip3"])
            self.assertIn("no sysroot", errors["wasm32-wasip3"])
            (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
            cells = {cell.configuration["id"]: cell for cell in build_cells(
                self.config, "test-platform", {"sha": "abc123"}, root / "worktree", root, {"example": "example-experiment"},
                aihc_store=root / "store", aihc_setup_errors=errors, capabilities=self.capabilities,
            )}
        self.assertIsNotNone(cells["aihc-wasm-O2"].setup_error)
        self.assertIsNone(cells["aihc-native-O2"].setup_error)
        self.assertIsNone(cells["aihc-llvm-O2"].setup_error)
        self.assertIsNone(cells["ghc-native-O2"].setup_error)

    def test_compile_records_time_and_size_and_measurement_carries_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = [cell for cell in self.build(root) if cell.configuration["id"] in {"ghc-native-O2", "aihc-native-O0"}]

            commands = []

            def fake_compile(command, cwd, timeout, environment=None):
                commands.append(command)
                if command[0] == "llvm-strip":
                    Path(command[-1]).write_bytes(b"bin")
                else:
                    Path(command[-1]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.runner.run_command", side_effect=fake_compile):
                compiled = compile_cells(cells, root, 30, 1)
            by_id = {cell.configuration["id"]: outcome for cell, outcome in compiled}
            self.assertEqual([command[0] for command in commands], ["/toolchains/bin/ghc-9.14.1", "llvm-strip"])
            # Artifact size is recorded after stripping.
            self.assertEqual(by_id["ghc-native-O2"]["artifact_bytes"], 3)
            self.assertTrue(by_id["ghc-native-O2"]["stripped"])
            self.assertGreater(by_id["ghc-native-O2"]["wall_time_ns"], 0)
            self.assertEqual(by_id["aihc-native-O0"], {"status": "unavailable", "reason": "missing_capability:optimization-flag"})

            def fake_measure(command, cwd, expected, timeout, threshold, maximum, invoke):
                return {"status": "converged", "metrics": [{"metric": "wall_time", "unit": "ns", "status": "ok", "estimate": 1, "samples": [1]}]}

            with patch("aihc_bench.runner.measure_adaptively", side_effect=fake_measure):
                results = {result["configuration"]: result for result in measure_cells(compiled, root, {"process_timeout_seconds": 1, "relative_threshold": 0.01, "maximum_bucket_size": 2})}
            self.assertEqual(results["ghc-native-O2"]["optimization"], "O2")
            self.assertEqual([item["metric"] for item in results["ghc-native-O2"]["measurement"]["metrics"]], ["wall_time", "compile_time", "artifact_size"])
            self.assertEqual(results["aihc-native-O0"]["measurement"], {"status": "unavailable", "reason": "missing_capability:optimization-flag"})

    def test_strip_tool_follows_the_artifact_kind(self):
        native, temporary = strip_command(Path("/a/program"))
        self.assertEqual(native, ["llvm-strip", "/a/program"])
        self.assertIsNone(temporary)
        wasm, temporary = strip_command(Path("/a/program.wasm"))
        self.assertEqual(wasm[:3], ["wasm-tools", "strip", "--all"])
        self.assertEqual(temporary, Path("/a/program.wasm.stripped"))

    def test_wasm_artifacts_are_stripped_through_a_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = [cell for cell in self.build(root) if cell.configuration["id"] == "aihc-wasm-O2"]

            def fake_compile(command, cwd, timeout, environment=None):
                if command[0] == "wasm-tools":
                    self.assertEqual(command[:3], ["wasm-tools", "strip", "--all"])
                    Path(command[-1]).write_bytes(b"small")
                else:
                    Path(command[command.index("--output") + 1]).write_bytes(b"large module")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.runner.run_command", side_effect=fake_compile):
                (cell, outcome), = compile_cells(cells, root, 30, 1)
            self.assertEqual(outcome["status"], "compiled")
            self.assertEqual(outcome["artifact_bytes"], 5)
            self.assertEqual(cell.artifact.read_bytes(), b"small")
            self.assertFalse(cell.artifact.with_name("program.wasm.stripped").exists())

    def test_strip_failure_fails_the_compile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = [cell for cell in self.build(root) if cell.configuration["id"] == "ghc-native-O2"]

            def fake_compile(command, cwd, timeout, environment=None):
                if command[0] == "llvm-strip":
                    return subprocess.CompletedProcess(command, 1, "", "not an object file")
                Path(command[-1]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.runner.run_command", side_effect=fake_compile):
                (_, outcome), = compile_cells(cells, root, 30, 1)
            self.assertEqual(outcome["status"], "compile_failed")
            self.assertIn("llvm-strip", outcome["stderr"])
            self.assertIn("not an object file", outcome["stderr"])

    def test_boot_equivalent_dependencies_from_real_benchmarks(self):
        config = load_config(REPO_ROOT / "benchmark.json")
        dependencies = _boot_equivalent_dependencies(config, REPO_ROOT)
        # snappy-roundtrip depends on bytestring (a GHC boot library AIHC
        # doesn't stand in for) and snappy-hs (not a boot library at all).
        self.assertIn("bytestring", dependencies)
        self.assertNotIn("snappy-hs", dependencies)
        self.assertNotIn("base", dependencies)  # implicit for AIHC, never installed explicitly


if __name__ == "__main__":
    unittest.main()
