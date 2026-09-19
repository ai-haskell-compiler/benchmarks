import os
import subprocess
import time
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from aihc_bench.config import load_config
from aihc_bench.git_history import GitError
from aihc_bench.runner import (
    INDEX_WARM_AGE_SECONDS,
    _archive_store,
    _restore_store,
    _configured_aihc_builds,
    _prepare_aihc_store,
    build_cells,
    build_compiler,
    MissingCorpus,
    runtime_is_package,
    compile_cells,
    hackage_index_cache,
    hackage_index_identity,
    hold_hackage_index,
    MissingBaseline,
    measure_cells,
    Phases,
    core_library_paths,
    require_baseline,
    reusable_baselines,
    reused_result,
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


def _core_libs(worktree: Path, names) -> Path:
    """A worktree whose core-libs holds these packages, cabal file and all."""
    for name in names:
        package = worktree / "core-libs" / name
        package.mkdir(parents=True, exist_ok=True)
        (package / f"{name}.cabal").write_text(f"name: {name}\n", encoding="utf-8")
    return worktree


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
                    "baseline": True,
                    "backend": "native",
                    "gc": "ghc-rts",
                    "optimization": "O2",
                    "runtime_stats": "ghc",
                    "compile": ["{toolchains}/bin/ghc-9.14.1", "{source}", "-o", "{artifact}"],
                    "run": ["{artifact}", "+RTS", "-t{stats_file}", "--machine-readable", "-RTS"],
                },
            ],
        }

    def build(self, root, store=None, archive=None):
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
                store_archive=archive,
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

    def test_corpus_benchmarks_get_the_directory_as_their_last_argument(self):
        self.config["benchmarks"].append(
            {"id": "sweep", "package": "sweep", "source": "example.hs", "corpus_env": "SWEEP_CORPUS", "expected_stdout": "ok\n"}
        )
        self.config["configurations"][1]["corpus_options"] = ["--dir", "{corpus}"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
            with patch.dict(os.environ, {"AIHC_BENCH_TOOLCHAINS": "/toolchains", "SWEEP_CORPUS": str(corpus)}):
                cells = build_cells(
                    self.config, "test-platform", {"sha": "abc123"}, root / "worktree", root,
                    {"example": "example-experiment", "sweep": "sweep-experiment"},
                )
        by_cell = {(cell.benchmark["id"], cell.configuration["id"]): cell for cell in cells}
        native = by_cell[("sweep", "aihc-native-O2")]
        self.assertEqual(native.run_command, [str(native.artifact), str(corpus)])
        wasm = by_cell[("sweep", "aihc-wasm-O2")]
        # The sandbox's own option to expose the directory goes before the
        # artifact; the program's argument goes last.
        self.assertEqual(
            wasm.run_command,
            ["wasmtime", "--env", f"AIHC_RTS_STATS={wasm.stats_file}", "--dir", str(corpus), str(wasm.artifact), str(corpus)],
        )
        ghc = by_cell[("sweep", "ghc-native-O2")]
        self.assertEqual(ghc.run_command[-1], str(corpus))
        # A benchmark without a corpus is untouched, corpus_options included.
        plain = by_cell[("example", "aihc-wasm-O2")]
        self.assertNotIn("--dir", plain.run_command)
        self.assertEqual(plain.run_command[-1], str(plain.artifact))

    def test_a_missing_corpus_stops_the_run_before_measuring(self):
        self.config["benchmarks"][0]["corpus_env"] = "SWEEP_CORPUS"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"AIHC_BENCH_TOOLCHAINS": "/toolchains"}, clear=False):
                os.environ.pop("SWEEP_CORPUS", None)
                with self.assertRaises(MissingCorpus):
                    self.build(root)
            with patch.dict(os.environ, {"SWEEP_CORPUS": str(root / "absent")}):
                with self.assertRaises(MissingCorpus):
                    self.build(root)

    def test_a_packaged_runtime_drops_the_gc_option(self):
        self.config["configurations"][0]["compile"] = ["aihc", "build", "{source}", "--gc", "semispace", "-O2", "-o", "{artifact_dir}"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cells = {cell.configuration["id"]: cell for cell in self.build(root)}
            self.assertIn("--gc", cells["aihc-native-O2"].compile_command)
            (root / "worktree" / "core-libs" / "aihc-rts").mkdir(parents=True)
            self.assertTrue(runtime_is_package(root / "worktree"))
            cells = {cell.configuration["id"]: cell for cell in self.build(root)}
            command = cells["aihc-native-O2"].compile_command
            self.assertNotIn("--gc", command)
            self.assertNotIn("semispace", command)
            self.assertEqual(command[:2] + command[3:], ["aihc", "build", "-O2", "-o", str(cells["aihc-native-O2"].artifact.parent), "--build-root", str(cells["aihc-native-O2"].build_dir)])

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
            _core_libs(worktree, ["aihc-base"])
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
        # Every core library, once per target and optimization level, since the
        # level is part of an installed package's identity.
        self.assertEqual(
            sorted(set(zip([command[command.index("--target") + 1] for command in installs], levels))),
            sorted({("test-native", "-O2"), ("wasm32-wasip3", "-O2"), ("llvm", "-O2"), ("test-native", "-O0"), ("test-native", "-O1"), ("test-native", "-Os")}),
        )
        # Preparing the runtime and installing the core libraries are
        # compilations too, so aihc gets every core there as well.
        for command in runtimes + installs:
            self.assertEqual(command[-3:], ["+RTS", "-N", "-RTS"])
        # A core library is a local path, so only --immutable puts it in the
        # store where aihc build resolves it as a core standin; without it the
        # install lands under the worktree and the build compiles it again.
        self.assertTrue(all("/core-libs/" in command[5] for command in installs))
        self.assertTrue(all("--immutable" in command for command in installs))
        self.assertEqual(run.call_args_list[1].args[3]["AIHC_WASM_CLANG"], "/toolchain/bin/clang")

    def test_a_packaged_runtime_is_installed_not_prepared(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            _core_libs(worktree, ["aihc-base", "aihc-rts"])
            with (
                patch.dict(os.environ, {"AIHC_BENCH_WASM_CLANG": "/toolchain/bin"}),
                patch("aihc_bench.runner.run_command", return_value=completed) as run,
            ):
                self.assertEqual(_prepare_aihc_store(self.config, "test-platform", worktree, root, root / "store", 30), {})
        commands = [call.args[0] for call in run.call_args_list]
        # aihc-prim depends on aihc-rts, so installing aihc-base builds the
        # runtime; there is no prepare-runtime command to call any more.
        self.assertEqual([command for command in commands if "prepare-runtime" in command], [])
        # Two core libraries across the six target/level combinations.
        self.assertEqual(len([command for command in commands if "install" in command]), 12)

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

    def test_the_aihc_store_is_reset_between_cells(self):
        """``aihc build`` installs a benchmark's dependencies into a store
        every cell of a commit shares. Left alone, the second benchmark to use
        bytestring in a configuration would find it installed and its compile
        time would not include installing it, while GHC's would."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "store"
            (store / "aihc-base").mkdir(parents=True)
            (store / "aihc-base" / "lib").write_bytes(b"prepared")
            archive = _archive_store(store)

            # What a timed compile leaves behind is gone at the next restore.
            (store / "bytestring").mkdir()
            (store / "bytestring" / "lib").write_bytes(b"installed by the last cell")
            cell = [cell for cell in self.build(root) if cell.configuration["id"] == "aihc-native-O2"][0]
            _restore_store(replace(cell, store_archive=archive))

            self.assertEqual((store / "aihc-base" / "lib").read_bytes(), b"prepared")
            self.assertFalse((store / "bytestring").exists())

    def test_only_aihc_cells_carry_the_store_archive(self):
        """GHC gets the same property from cabal's per-configuration store and
        the build directory wipe: it simply starts empty."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "store"
            store.mkdir()
            by_id = {cell.configuration["id"]: cell for cell in self.build(root, archive=_archive_store(store))}
            self.assertIsNotNone(by_id["aihc-native-O2"].store_archive)
            self.assertIsNone(by_id["ghc-native-O2"].store_archive)

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

    def test_every_core_library_is_installed_ahead_of_the_timed_compile(self):
        """Installing a dependency is part of what a compiler is timed doing.

        The real suite has benchmarks depending on bytestring and text, which
        GHC ships and AIHC does not; both compilers install them inside the
        timed compile. The core libraries are the exception on the AIHC side,
        as base and the other wired-in packages are on the GHC side: they are
        the compiler's own, and no benchmark builds them either.

        Preparing aihc-base alone left the rest of that set inside the timed
        compile, so a benchmark reaching bytestring rebuilt aihc-internal and
        aihc-template-haskell -- about 11 MB of core library -- in every cell,
        while GHC never rebuilds its wired-in closure at all.
        """
        completed = subprocess.CompletedProcess([], 0, "", "")
        config = load_config(REPO_ROOT / "benchmark.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            names = ["aihc-base", "aihc-internal", "aihc-prim", "aihc-rts", "aihc-template-haskell"]
            _core_libs(worktree, names)
            with patch("aihc_bench.runner.run_command", return_value=completed) as run:
                _prepare_aihc_store(config, "aarch64-darwin", worktree, REPO_ROOT, root / "store", 30)
        installed = {command[5] for command in (call.args[0] for call in run.call_args_list) if "install" in command}
        self.assertEqual(installed, {str(worktree / "core-libs" / name) for name in names})



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

    def test_holding_the_index_keeps_a_long_run_on_one_hackage(self):
        """AIHC refetches a day-old index. A sweep runs for days, so without
        this the commits measured after the refresh resolve against a
        different Hackage than the ones before it, inside one series."""
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            derived = cache / "preferred-versions.txt"
            derived.write_text("a 1.0\n", encoding="utf-8")
            table = cache / "index.txt"
            table.write_text("a 1.0 entry\n", encoding="utf-8")
            (cache / "01-index.tar").write_bytes(b"tar")
            stale = time.time() - 40 * 60 * 60
            os.utime(table, (stale, stale))
            with patch("aihc_bench.runner.hackage_index_cache", return_value=derived):
                hold_hackage_index()
            self.assertLess(time.time() - table.stat().st_mtime, 60)

    def test_holding_an_incomplete_index_does_nothing(self):
        """Without the tarball AIHC treats the cache as incomplete and
        refetches whatever the table's timestamp says, so touching the table
        would only hide that the cache needs rebuilding."""
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            derived = cache / "preferred-versions.txt"
            derived.write_text("a 1.0\n", encoding="utf-8")
            table = cache / "index.txt"
            table.write_text("a 1.0 entry\n", encoding="utf-8")
            stale = time.time() - 40 * 60 * 60
            os.utime(table, (stale, stale))
            with patch("aihc_bench.runner.hackage_index_cache", return_value=derived):
                hold_hackage_index()
            self.assertGreater(time.time() - table.stat().st_mtime, 30 * 60 * 60)

    def test_the_index_identity_changes_with_the_index(self):
        """Two machines resolving against different indexes published numbers
        that looked comparable, because a result said nothing about which
        Hackage chose its versions."""
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            derived = cache / "preferred-versions.txt"
            derived.write_text("a 1.0\n", encoding="utf-8")
            (cache / "index.txt").write_text("a 1.0 entry\n", encoding="utf-8")
            with patch("aihc_bench.runner.hackage_index_cache", return_value=derived):
                first = hackage_index_identity()
                derived.write_text("a 1.1\n", encoding="utf-8")
                second = hackage_index_identity()
            self.assertIsNotNone(first)
            self.assertNotEqual(first["sha256"], second["sha256"])

    def test_the_index_identity_is_absent_without_a_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "preferred-versions.txt"
            with patch("aihc_bench.runner.hackage_index_cache", return_value=derived):
                self.assertIsNone(hackage_index_identity())

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


class BaselineTests(unittest.TestCase):
    """Every AIHC number is a ratio against GHC.

    A commit measured without a baseline is not a partial result but a useless
    one, and recording it as available hides a broken machine behind a
    benchmark that merely looks empty -- one M1 published seventeen commits of
    AIHC-only results that way.
    """

    def _pair(self, benchmark, configuration, status, **extra):
        cell = SimpleNamespace(benchmark={"id": benchmark}, configuration=configuration)
        return (cell, {"status": status, **extra})

    BASELINE = {"id": "ghc-9.14.1-native-O2", "baseline": True}
    OTHER_BASELINE = {"id": "ghc-9.14.1-llvm-O2", "baseline": True}
    # Every configured GHC configuration is a baseline, so this one is
    # hypothetical: it guards the branch, not a configuration that exists.
    NOT_BASELINE = {"id": "ghc-9.14.1-native-O2-unblessed"}
    AIHC = {"id": "aihc-native-semispace-O2"}

    def test_a_compiled_baseline_is_enough(self):
        compiled = [self._pair("example", self.BASELINE, "compiled")]
        require_baseline(compiled, ["example"])

    def test_one_baseline_surviving_is_enough(self):
        """A single broken backend is a partial result, not a useless one."""
        compiled = [
            self._pair("example", self.BASELINE, "compiled"),
            self._pair("example", self.OTHER_BASELINE, "compile_failed", stderr="llvm missing"),
        ]
        require_baseline(compiled, ["example"])

    def test_every_baseline_failing_stops_the_run(self):
        compiled = [
            self._pair("example", self.BASELINE, "compile_failed", stderr="ghc-pkg ... not found\nsecond line"),
            self._pair("example", self.AIHC, "compiled"),
        ]
        with self.assertRaises(MissingBaseline) as raised:
            require_baseline(compiled, ["example"])
        message = str(raised.exception)
        self.assertIn("example", message)
        self.assertIn("ghc-9.14.1-native-O2", message)
        self.assertIn("ghc-pkg", message)
        self.assertNotIn("second line", message)

    def test_a_non_baseline_ghc_does_not_substitute(self):
        """Ratios are computed against the baseline, not any GHC at all.

        No unblessed GHC configuration is configured today, so this guards
        the branch against one being added without `baseline: true`.
        """
        compiled = [
            self._pair("example", self.BASELINE, "compile_failed", stderr="boom"),
            self._pair("example", self.NOT_BASELINE, "compiled"),
        ]
        with self.assertRaises(MissingBaseline):
            require_baseline(compiled, ["example"])

    def test_a_benchmark_with_no_baseline_cells_stops_the_run(self):
        compiled = [self._pair("example", self.AIHC, "compiled")]
        with self.assertRaises(MissingBaseline) as raised:
            require_baseline(compiled, ["example"])
        self.assertIn("no baseline configuration ran", str(raised.exception))

    def test_each_benchmark_is_checked(self):
        """snappy-roundtrip served no GHC series for two days while the other
        benchmarks on the same machine were fine."""
        compiled = [
            self._pair("fine", self.BASELINE, "compiled"),
            self._pair("broken", self.BASELINE, "compile_failed", stderr="unknown package: snappy-hs"),
        ]
        require_baseline(compiled, ["fine"])
        with self.assertRaises(MissingBaseline) as raised:
            require_baseline(compiled, ["fine", "broken"])
        self.assertIn("broken", str(raised.exception))


class BaselineReuseTests(unittest.TestCase):
    """A GHC configuration's inputs are the benchmark, the toolchain and the
    machine -- never the AIHC commit under test. Measuring it again for every
    AIHC commit re-derives a number that cannot have moved."""

    EXPERIMENTS = {"example": "example-v1-abc"}
    ENVIRONMENT = {"id": "test-platform-aaaa"}

    def _database(self, entries, environment_id="test-platform-aaaa", finished_at="2026-09-19T12:00:00"):
        class Stub:
            def results_measured_since(self, experiment_id, platform_id, wanted_id, since):
                if wanted_id != environment_id or finished_at < since:
                    return []
                return [dict(entry, _measured_at=finished_at, _measured_for="cafe" * 10) for entry in entries]

        return Stub()

    def _ghc(self, configuration="ghc-9.14.1-native-O2", status="ok"):
        return {
            "benchmark": "example",
            "configuration": configuration,
            "compiler_family": "ghc",
            "baseline": True,
            "compile": {"status": "compiled", "wall_time_ns": 3_000_000_000},
            "measurement": {"status": status, "metrics": []},
        }

    def test_a_recent_ghc_result_is_reused(self):
        database = self._database([self._ghc()])
        reusable = reusable_baselines(database, {}, self.EXPERIMENTS, "test-platform", self.ENVIRONMENT)
        self.assertEqual(list(reusable), [("example", "ghc-9.14.1-native-O2")])

    def test_aihc_is_never_reused(self):
        """Its compiler is the commit under test, which is the whole point."""
        aihc = dict(self._ghc(configuration="aihc-native-semispace-O2"), compiler_family="aihc")
        database = self._database([aihc])
        self.assertEqual(reusable_baselines(database, {}, self.EXPERIMENTS, "test-platform", self.ENVIRONMENT), {})

    def test_a_failed_result_is_not_reused(self):
        """A failure is a question about this machine now."""
        database = self._database([self._ghc(status="unavailable")])
        self.assertEqual(reusable_baselines(database, {}, self.EXPERIMENTS, "test-platform", self.ENVIRONMENT), {})

    def test_another_environment_does_not_supply_a_baseline(self):
        database = self._database([self._ghc()], environment_id="other-environment")
        self.assertEqual(reusable_baselines(database, {}, self.EXPERIMENTS, "test-platform", self.ENVIRONMENT), {})

    def test_reuse_can_be_switched_off(self):
        database = self._database([self._ghc()])
        config = {"baseline_reuse_hours": 0}
        self.assertEqual(reusable_baselines(database, config, self.EXPERIMENTS, "test-platform", self.ENVIRONMENT), {})

    def test_a_result_older_than_the_window_is_measured_again(self):
        """The machine drifts even when the benchmark and toolchain do not."""
        database = self._database([self._ghc()], finished_at="2020-01-01T00:00:00")
        self.assertEqual(reusable_baselines(database, {}, self.EXPERIMENTS, "test-platform", self.ENVIRONMENT), {})

    def test_a_reused_result_says_where_it_came_from(self):
        """Its compile time is another moment's, so the entry has to say so."""
        entry = dict(self._ghc(), _measured_at="2026-09-19T12:00:00", _measured_for="f" * 40)
        result = reused_result(entry)
        self.assertEqual(result["reused_from"], {"commit_sha": "f" * 40, "measured_at": "2026-09-19T12:00:00"})
        self.assertNotIn("_measured_at", result)
        self.assertEqual(result["compile"]["wall_time_ns"], 3_000_000_000)

    def test_a_reused_baseline_satisfies_the_guard(self):
        """It compiled and ran on this machine, inside the window."""
        cell = SimpleNamespace(benchmark={"id": "example"}, configuration={"id": "aihc-native-semispace-O2"})
        compiled = [(cell, {"status": "compiled"})]
        require_baseline(compiled, ["example"], satisfied={"example"})

    def test_without_a_reused_baseline_the_guard_still_stops_the_run(self):
        cell = SimpleNamespace(benchmark={"id": "example"}, configuration={"id": "aihc-native-semispace-O2"})
        with self.assertRaises(MissingBaseline):
            require_baseline([(cell, {"status": "compiled"})], ["example"], satisfied=set())


class PhaseTimingTests(unittest.TestCase):
    """A commit's cost was visible per cell only, so building the compiler and
    preparing its stores -- which happen once per commit and can dominate it --
    left no trace."""

    def test_each_phase_is_timed_separately(self):
        phases = Phases()
        with phases.timing("compiler_build"):
            time.sleep(0.01)
        with phases.timing("measure"):
            pass
        record = phases.record()
        self.assertEqual(sorted(record["phases_ns"]), ["compiler_build", "measure"])
        self.assertGreater(record["phases_ns"]["compiler_build"], record["phases_ns"]["measure"])
        self.assertEqual(record["total_ns"], sum(record["phases_ns"].values()))

    def test_a_phase_entered_twice_accumulates(self):
        phases = Phases()
        for _ in range(2):
            with phases.timing("compile"):
                pass
        self.assertEqual(len(phases.elapsed_ns), 1)

    def test_a_failing_phase_is_still_timed(self):
        """A commit that dies in the compiler build is exactly the one whose
        cost needs explaining."""
        phases = Phases()
        with self.assertRaises(ValueError):
            with phases.timing("compiler_build"):
                raise ValueError("boom")
        self.assertIn("compiler_build", phases.elapsed_ns)


class CoreLibraryPreparationTests(unittest.TestCase):
    """GHC never rebuilds its wired-in closure for a benchmark, and AIHC's own
    lock marks the same set "source": "core". Preparing only aihc-base left
    the rest inside the timed compile: a benchmark reaching bytestring pulled
    in aihc-internal and aihc-template-haskell, about 11 MB of core library,
    rebuilt for every cell."""

    def _worktree(self, root, names):
        core = Path(root) / "core-libs"
        core.mkdir(parents=True)
        for name in names:
            package = core / name
            package.mkdir()
            (package / f"{name}.cabal").write_text("name: " + name, encoding="utf-8")
        return Path(root)

    def test_every_core_library_is_prepared(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = self._worktree(
                directory, ["aihc-base", "aihc-internal", "aihc-prim", "aihc-rts", "aihc-template-haskell"]
            )
            names = [path.name for path in core_library_paths(worktree)]
            self.assertEqual(sorted(names[1:]), ["aihc-internal", "aihc-prim", "aihc-rts", "aihc-template-haskell"])

    def test_aihc_base_is_prepared_first(self):
        """Nothing on a target builds without it, so its failure is the one
        that makes the target unavailable."""
        with tempfile.TemporaryDirectory() as directory:
            worktree = self._worktree(directory, ["aihc-template-haskell", "aihc-base", "aihc-internal"])
            self.assertEqual(core_library_paths(worktree)[0].name, "aihc-base")

    def test_the_set_is_read_from_the_tree(self):
        """It moves with the compiler: aihc-rts became one of them in #2142."""
        with tempfile.TemporaryDirectory() as directory:
            worktree = self._worktree(directory, ["aihc-base", "aihc-future-library"])
            self.assertIn("aihc-future-library", [path.name for path in core_library_paths(worktree)])

    def test_a_directory_without_a_cabal_file_is_not_a_package(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = self._worktree(directory, ["aihc-base"])
            (worktree / "core-libs" / "scratch").mkdir()
            self.assertEqual([path.name for path in core_library_paths(worktree)], ["aihc-base"])

    def test_no_core_libs_directory_prepares_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(core_library_paths(Path(directory)), [])
