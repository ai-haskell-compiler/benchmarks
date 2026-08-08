import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.runner import (
    _configured_aihc_targets,
    _prepare_aihc_store,
    _requires_installed_store,
    build_cells,
)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "platforms": {"test-platform": {"aihc_native_target": "test-native"}},
            "benchmarks": [
                {"id": "example", "source": "example.hs", "expected_stdout": "ok\n"}
            ],
            "configurations": [
                {
                    "id": "aihc-native",
                    "compiler_family": "aihc",
                    "compiler_version": "commit",
                    "backend": "native",
                    "gc": "semispace",
                    "aihc_target": "{aihc_native_target}",
                    "compile": ["aihc", "compile", "{source}", "--output", "{artifact}"],
                    "run": ["{artifact}"],
                },
                {
                    "id": "aihc-wasm",
                    "compiler_family": "aihc",
                    "compiler_version": "commit",
                    "backend": "wasm",
                    "gc": "semispace",
                    "aihc_target": "wasm32-wasip3",
                    "compile_path_env": "AIHC_BENCH_WASM_CLANG",
                    "compile": ["aihc", "compile", "{source}", "--output", "{artifact}"],
                    "run": ["wasmtime", "{artifact}"],
                },
                {
                    "id": "aihc-llvm",
                    "compiler_family": "aihc",
                    "compiler_version": "commit",
                    "backend": "llvm",
                    "gc": "semispace",
                    "aihc_target": "llvm",
                    "compile": ["aihc", "compile", "{source}", "--output", "{artifact}"],
                    "run": ["{artifact}"],
                },
                {
                    "id": "ghc-native",
                    "compiler_family": "ghc",
                    "compiler_version": "9.14.1",
                    "backend": "native",
                    "gc": "ghc-rts",
                    "compile": ["ghc", "{source}", "-o", "{artifact}"],
                    "run": ["{artifact}"],
                },
            ],
        }

    def test_detects_installed_store_cli(self):
        self.assertTrue(_requires_installed_store("Commands: prepare-runtime", ""))
        self.assertFalse(_requires_installed_store("Commands: compile", ""))

    def test_expands_configured_aihc_targets(self):
        with patch.dict(os.environ, {"AIHC_BENCH_WASM_CLANG": "/toolchain/bin"}):
            targets = _configured_aihc_targets(self.config, "test-platform")

        self.assertEqual([target for target, _, _ in targets], ["test-native", "wasm32-wasip3", "llvm"])
        self.assertEqual(targets[1][2]["AIHC_WASM_CLANG"], "/toolchain/bin/clang")

    def test_store_is_added_only_to_aihc_compile_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.hs").write_text("main = putStrLn \"ok\"\n")
            store = root / "store"
            cells = build_cells(
                self.config,
                "test-platform",
                {"sha": "abc123"},
                root / "worktree",
                root,
                "experiment",
                aihc_store=store,
            )

        for cell in cells:
            command = cell.compile_command or []
            if cell.configuration["compiler_family"] == "aihc":
                self.assertEqual(command[-2:], ["--store", str(store)])
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
                error = _prepare_aihc_store(self.config, "test-platform", worktree, store, 30)

        self.assertIsNone(error)
        self.assertEqual(run.call_count, 4)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            [(command[command.index("--target") + 1], command[command.index("--gc") + 1]) for command in commands[:3]],
            [("test-native", "semispace"), ("wasm32-wasip3", "semispace"), ("llvm", "semispace")],
        )
        self.assertEqual(
            [commands[3][index + 1] for index, value in enumerate(commands[3]) if value == "--target"],
            ["test-native", "wasm32-wasip3", "llvm"],
        )
        self.assertEqual(run.call_args_list[1].args[3]["AIHC_WASM_CLANG"], "/toolchain/bin/clang")


if __name__ == "__main__":
    unittest.main()
