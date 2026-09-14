import os
import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.config import load_config
from aihc_bench.git_history import GitError
from aihc_bench.runner import (
    INDEX_WARM_AGE_SECONDS,
    _boot_equivalent_dependencies,
    _configured_aihc_builds,
    _prepare_aihc_store,
    build_cells,
    build_compiler,
    compile_cells,
    hackage_index_cache,
    measure_cells,
    strip_command,
    warm_hackage_index,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        "compile": ["aihc", "build", "{source}", f"-{profile}", "-o", "{artifact_dir}"],
        "run": ["wasmtime", "--env", "AIHC_RTS_STATS={stats_file}", "{artifact}"] if backend == "wasm" else ["{artifact}"],
    }
    entry.update(extra)
    return entry


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "platforms": {"test-platform": {"aihc_native_target": "test-native"}},
            "benchmarks": [{"id": "example", "package": "example", "source": "example.hs", "expected_stdout": "ok\n"}],
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

    def build(self, root, store=None):
        (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
        with patch.dict(os.environ, {"AIHC_BENCH_TOOLCHAINS": "/toolchains"}):
            return build_cells(
                self.config,
                "test-platform",
                {"sha": "abc123"},
                root / "worktree",
                root,
                {"example": "example-experiment"},
                aihc_store=store,
            )

    def test_build_compiler_reports_only_a_failed_build(self):
        with patch("aihc_bench.runner.run_command", return_value=subprocess.CompletedProcess([], 0, "help", "")) as run:
            self.assertIsNone(build_compiler(Path("/wt"), Path("/root"), 30))
        self.assertEqual(run.call_args.args[0], ["nix", "run", "/wt#aihc", "--", "--help"])
        with patch("aihc_bench.runner.run_command", return_value=subprocess.CompletedProcess([], 1, "", "boom")):
            self.assertEqual(build_compiler(Path("/wt"), Path("/root"), 30), "boom")

    def test_cells_cover_only_the_requested_benchmarks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
            config = {**self.config, "benchmarks": self.config["benchmarks"] + [{"id": "other", "package": "other", "source": "example.hs", "expected_stdout": "ok\n"}]}
            cells = build_cells(config, "test-platform", {"sha": "abc123"}, root / "worktree", root, {"other": "other-exp"})
            self.assertEqual({cell.benchmark["id"] for cell in cells}, {"other"})
            # Artifacts and stats are cached under the benchmark's own experiment.
            self.assertIn("other-exp", str(cells[0].artifact))
            self.assertEqual(build_cells(config, "test-platform", {"sha": "abc123"}, root / "worktree", root, {}), [])

    def test_aihc_cells_build_the_package_into_the_artifact_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = {cell.configuration["id"]: cell for cell in self.build(root)}
            native = cells["aihc-native-O2"]
            # aihc names the executable after the package's executable stanza.
            self.assertEqual(native.artifact.name, "example")
            self.assertEqual(cells["aihc-wasm-O2"].artifact.name, "example.wasm")
            self.assertIn(str(native.artifact.parent), native.compile_command)
            for profile in ("O0", "O1", "Os"):
                self.assertIn(f"-{profile}", cells[f"aihc-native-{profile}"].compile_command)
            self.assertEqual(cells["ghc-native-O2"].artifact.name, "program")

    def test_every_aihc_cell_gets_its_own_build_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = {cell.configuration["id"]: cell for cell in self.build(root)}
            native = cells["aihc-native-O2"]
            self.assertEqual(native.compile_command[-2:], ["--build-root", str(native.build_dir)])
            self.assertNotIn("--build-root", cells["ghc-native-O2"].compile_command)

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

    def test_expands_configured_aihc_builds(self):
        with patch.dict(os.environ, {"AIHC_BENCH_WASM_CLANG": "/toolchain/bin"}):
            builds = _configured_aihc_builds(self.config, "test-platform")

        self.assertEqual(
            [(target, optimization) for target, _, optimization, _ in builds],
            [("test-native", "O2"), ("wasm32-wasip3", "O2"), ("llvm", "O2"), ("test-native", "O0"), ("test-native", "O1"), ("test-native", "Os")],
        )
        self.assertEqual(builds[1][3]["AIHC_WASM_CLANG"], "/toolchain/bin/clang")

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
                self.assertEqual(_prepare_aihc_store(self.config, "test-platform", worktree, root, store, 30), {})

        commands = [call.args[0] for call in run.call_args_list]
        # One runtime per target and GC, whatever optimization levels use it.
        runtimes = [command for command in commands if "prepare-runtime" in command]
        self.assertEqual(
            [(command[command.index("--target") + 1], command[command.index("--gc") + 1]) for command in runtimes],
            [("test-native", "semispace"), ("wasm32-wasip3", "semispace"), ("llvm", "semispace")],
        )
        # aihc-base once per target and optimization level, since the level is
        # part of an installed package's identity.
        installs = [command for command in commands if "install" in command]
        levels = [next(item for item in command if item.startswith("-O")) for command in installs]
        self.assertEqual(
            list(zip([command[command.index("--target") + 1] for command in installs], levels)),
            [("test-native", "-O2"), ("wasm32-wasip3", "-O2"), ("llvm", "-O2"), ("test-native", "-O0"), ("test-native", "-O1"), ("test-native", "-Os")],
        )
        # Preparing the runtime and installing the core libraries are
        # compilations too, so aihc gets every core there as well.
        for command in runtimes + installs:
            self.assertEqual(command[-3:], ["+RTS", "-N", "-RTS"])
        # aihc-base is a local path, so only --immutable puts it in the store
        # where aihc build resolves it as a core standin; without it the
        # install lands under the worktree and the build compiles base again.
        self.assertTrue(all(command[5].endswith("core-libs/aihc-base") for command in installs))
        self.assertTrue(all("--immutable" in command for command in installs))
        self.assertEqual(run.call_args_list[1].args[3]["AIHC_WASM_CLANG"], "/toolchain/bin/clang")

    def test_failed_wasm_preparation_only_affects_wasm_cells(self):
        def fake_run(command, cwd, timeout, environment=None):
            if "prepare-runtime" in command and "wasm32-wasip3" in command:
                return subprocess.CompletedProcess(command, 1, "", "no sysroot")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("aihc_bench.runner.run_command", side_effect=fake_run):
                errors = _prepare_aihc_store(self.config, "test-platform", root / "worktree", root, root / "store", 30)
            self.assertEqual(list(errors), ["wasm32-wasip3"])
            self.assertIn("no sysroot", errors["wasm32-wasip3"])
            (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
            cells = {cell.configuration["id"]: cell for cell in build_cells(
                self.config, "test-platform", {"sha": "abc123"}, root / "worktree", root, {"example": "example-experiment"},
                aihc_store=root / "store", aihc_setup_errors=errors,
            )}
        self.assertIsNotNone(cells["aihc-wasm-O2"].setup_error)
        self.assertIsNone(cells["aihc-native-O2"].setup_error)
        self.assertIsNone(cells["aihc-llvm-O2"].setup_error)
        self.assertIsNone(cells["ghc-native-O2"].setup_error)

    def test_compile_records_time_and_size_and_measurement_carries_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = [cell for cell in self.build(root) if cell.configuration["id"] == "ghc-native-O2"]

            commands = []

            def fake_compile(command, cwd, timeout, environment=None):
                commands.append(command)
                if command[0] == "llvm-strip":
                    Path(command[-1]).write_bytes(b"bin")
                else:
                    Path(command[-1]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.runner.run_command", side_effect=fake_compile):
                compiled = compile_cells(cells, root, 30)
            by_id = {cell.configuration["id"]: outcome for cell, outcome in compiled}
            self.assertEqual([command[0] for command in commands], ["/toolchains/bin/ghc-9.14.1", "llvm-strip"])
            # Artifact size is recorded after stripping.
            self.assertEqual(by_id["ghc-native-O2"]["artifact_bytes"], 3)
            self.assertTrue(by_id["ghc-native-O2"]["stripped"])
            self.assertGreater(by_id["ghc-native-O2"]["wall_time_ns"], 0)

            def fake_measure(command, cwd, expected, timeout, threshold, maximum, invoke):
                return {"status": "converged", "metrics": [{"metric": "wall_time", "unit": "ns", "status": "ok", "estimate": 1, "samples": [1]}]}

            with patch("aihc_bench.runner.measure_adaptively", side_effect=fake_measure):
                results = {result["configuration"]: result for result in measure_cells(compiled, root, {"process_timeout_seconds": 1, "relative_threshold": 0.01, "maximum_bucket_size": 2})}
            self.assertEqual(results["ghc-native-O2"]["optimization"], "O2")
            self.assertEqual([item["metric"] for item in results["ghc-native-O2"]["measurement"]["metrics"]], ["wall_time", "compile_time", "artifact_size"])

    def test_every_compile_is_timed_from_scratch(self):
        """A reused artifact has no compile time, and a warm build directory
        would time an incremental no-op rather than a compile."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = [cell for cell in self.build(root) if cell.configuration["id"] == "ghc-native-O2"]
            cell = cells[0]
            cell.artifact.parent.mkdir(parents=True, exist_ok=True)
            cell.artifact.write_bytes(b"stale artifact")
            cell.build_dir.mkdir(parents=True, exist_ok=True)
            (cell.build_dir / "dist").mkdir()
            (cell.build_dir / "dist" / "warm").write_bytes(b"incremental state")

            commands = []

            def fake_compile(command, cwd, timeout, environment=None):
                commands.append(command[0])
                if command[0] == "llvm-strip":
                    Path(command[-1]).write_bytes(b"bin")
                else:
                    self.assertFalse((cell.build_dir / "dist").exists(), "build directory was not cleared")
                    Path(command[-1]).write_bytes(b"binary")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.runner.run_command", side_effect=fake_compile):
                (_, outcome), = compile_cells(cells, root, 30)
            self.assertEqual(commands, ["/toolchains/bin/ghc-9.14.1", "llvm-strip"])
            self.assertNotIn("cached", outcome)
            self.assertGreater(outcome["wall_time_ns"], 0)

    def test_compiles_run_one_at_a_time(self):
        """Compile time is published, so a compile owns the machine.

        Concurrent compiles made compile time a measure of how many other
        compilers happened to be running; with -N each of them takes every
        core, so a 32-core machine defaulted to 32 compilers on 32 cores.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = self.build(root)
            concurrent = 0
            peak = 0

            def fake_compile(command, cwd, timeout, environment=None):
                nonlocal concurrent, peak
                concurrent += 1
                peak = max(peak, concurrent)
                try:
                    if command[0] in ("llvm-strip", "wasm-tools"):
                        Path(command[-1]).write_bytes(b"bin")
                    else:
                        target = Path(command[command.index("-o") + 1]) if "-o" in command else Path(command[-1])
                        if target.is_dir() or command[0].endswith("aihc"):
                            target.mkdir(parents=True, exist_ok=True)
                            (target / "example").write_bytes(b"binary")
                        else:
                            Path(command[-1]).write_bytes(b"binary")
                    return subprocess.CompletedProcess(command, 0, "", "")
                finally:
                    concurrent -= 1

            with patch("aihc_bench.runner.run_command", side_effect=fake_compile):
                compile_cells(cells, root, 30)
            self.assertEqual(peak, 1, "compiles overlapped")

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
                    Path(command[command.index("-o") + 1], "example.wasm").write_bytes(b"large module")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("aihc_bench.runner.run_command", side_effect=fake_compile):
                (cell, outcome), = compile_cells(cells, root, 30)
            self.assertEqual(outcome["status"], "compiled")
            self.assertEqual(outcome["artifact_bytes"], 5)
            self.assertEqual(cell.artifact.read_bytes(), b"small")
            self.assertFalse(cell.artifact.with_name("example.wasm.stripped").exists())

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
                (_, outcome), = compile_cells(cells, root, 30)
            self.assertEqual(outcome["status"], "compile_failed")
            self.assertIn("llvm-strip", outcome["stderr"])
            self.assertIn("not an object file", outcome["stderr"])

    def test_boot_equivalent_dependencies_from_real_benchmarks(self):
        config = load_config(REPO_ROOT / "benchmark.json")
        dependencies = _boot_equivalent_dependencies(config, REPO_ROOT)
        self.assertIsInstance(dependencies, list)
        # snappy-roundtrip depends on bytestring (a GHC boot library AIHC
        # doesn't stand in for) and snappy-hs (not a boot library at all).
        self.assertIn("bytestring", dependencies)
        self.assertNotIn("snappy-hs", dependencies)
        self.assertNotIn("base", dependencies)  # implicit for AIHC, never installed explicitly



class HackageIndexWarmingTests(unittest.TestCase):
    """A measured commit must never refresh AIHC's Hackage index itself.

    The refresh runs the measured compiler's own code, so a commit from before
    ai-haskell-compiler/aihc#2057 heap-overflows on it. Whether a historical
    commit built would otherwise depend on how old the cache happened to be
    when it was scheduled, and a failure is terminal until ``forget``.
    """

    config = {
        "aihc_ref": "origin/main",
        "platforms": {"test-platform": {"aihc_native_target": "test-native"}},
    }

    def test_a_fresh_index_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "preferred-versions.txt"
            derived.write_text("a 1.0\n", encoding="utf-8")
            with (
                patch("aihc_bench.runner.hackage_index_cache", return_value=derived),
                patch("aihc_bench.runner.run_command") as run,
            ):
                self.assertIsNone(warm_hackage_index(self.config, "test-platform", Path("/repo"), Path("/root"), 30))
            run.assert_not_called()

    def test_warming_fetches_before_resolving_the_ref(self):
        """aihc_ref resolves against the local clone.

        A clone last fetched before the fix landed resolves to a compiler that
        still retains the tarball, so warming would build the very compiler it
        exists to keep away from the refresh -- which is exactly what happened
        on worker-desktop.
        """
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "absent.txt"
            order = []
            with (
                patch("aihc_bench.runner.hackage_index_cache", return_value=derived),
                patch("aihc_bench.runner.fetch", side_effect=lambda r: order.append("fetch")),
                patch("aihc_bench.runner.rev_parse", side_effect=lambda r, ref: order.append("resolve") or "f" * 40),
                patch("aihc_bench.runner.create_worktree"),
                patch("aihc_bench.runner.remove_worktree"),
                patch("aihc_bench.runner.build_compiler", return_value=None),
                patch("aihc_bench.runner.run_command", return_value=subprocess.CompletedProcess([], 0, "", "")),
            ):
                warm_hackage_index(self.config, "test-platform", Path("/repo"), Path(directory) / "root", 30)
            self.assertEqual(order, ["fetch", "resolve"])

    def test_warming_survives_a_fetch_failure(self):
        """The ref already in the clone is still the best available."""
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "absent.txt"
            with (
                patch("aihc_bench.runner.hackage_index_cache", return_value=derived),
                patch("aihc_bench.runner.fetch", side_effect=GitError("no network")),
                patch("aihc_bench.runner.rev_parse", return_value="f" * 40),
                patch("aihc_bench.runner.create_worktree"),
                patch("aihc_bench.runner.remove_worktree"),
                patch("aihc_bench.runner.build_compiler", return_value=None),
                patch("aihc_bench.runner.run_command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
            ):
                self.assertIsNone(
                    warm_hackage_index(self.config, "test-platform", Path("/repo"), Path(directory) / "root", 30)
                )
            run.assert_called_once()

    def test_a_stale_index_is_refreshed_with_the_current_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "preferred-versions.txt"
            derived.write_text("a 1.0\n", encoding="utf-8")
            old = time.time() - INDEX_WARM_AGE_SECONDS - 60
            os.utime(derived, (old, old))
            root = Path(directory) / "root"
            with (
                patch("aihc_bench.runner.hackage_index_cache", return_value=derived),
                patch("aihc_bench.runner.fetch"),
                patch("aihc_bench.runner.rev_parse", return_value="f" * 40),
                patch("aihc_bench.runner.create_worktree"),
                patch("aihc_bench.runner.remove_worktree") as remove,
                patch("aihc_bench.runner.build_compiler", return_value=None),
                patch(
                    "aihc_bench.runner.run_command",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as run,
            ):
                self.assertIsNone(warm_hackage_index(self.config, "test-platform", Path("/repo"), root, 30))
            command = run.call_args.args[0]
            self.assertEqual(command[4:6], ["install", "bytestring"])
            self.assertIn("--target", command)
            self.assertEqual(command[command.index("--target") + 1], "test-native")
            # The worktree is cleaned up even on the happy path.
            remove.assert_called_once()

    def test_a_missing_index_is_fetched(self):
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "absent.txt"
            with (
                patch("aihc_bench.runner.hackage_index_cache", return_value=derived),
                patch("aihc_bench.runner.fetch"),
                patch("aihc_bench.runner.rev_parse", return_value="f" * 40),
                patch("aihc_bench.runner.create_worktree"),
                patch("aihc_bench.runner.remove_worktree"),
                patch("aihc_bench.runner.build_compiler", return_value=None),
                patch("aihc_bench.runner.run_command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
            ):
                self.assertIsNone(
                    warm_hackage_index(self.config, "test-platform", Path("/repo"), Path(directory) / "root", 30)
                )
            run.assert_called_once()

    def test_a_failed_warming_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "absent.txt"
            with (
                patch("aihc_bench.runner.hackage_index_cache", return_value=derived),
                patch("aihc_bench.runner.fetch"),
                patch("aihc_bench.runner.rev_parse", return_value="f" * 40),
                patch("aihc_bench.runner.create_worktree"),
                patch("aihc_bench.runner.remove_worktree") as remove,
                patch("aihc_bench.runner.build_compiler", return_value=None),
                patch("aihc_bench.runner.run_command", return_value=subprocess.CompletedProcess([], 1, "", "boom")),
            ):
                self.assertEqual(
                    warm_hackage_index(self.config, "test-platform", Path("/repo"), Path(directory) / "root", 30),
                    "boom",
                )
            remove.assert_called_once()

    def test_the_cache_path_follows_xdg(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": "/xdg"}):
            self.assertEqual(hackage_index_cache(), Path("/xdg/aihc/hackage-index/preferred-versions.txt"))


if __name__ == "__main__":
    unittest.main()
