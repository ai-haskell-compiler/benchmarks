import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.toolchain import (
    CompilerDoesNotBuild,
    MachineFault,
    build_compiler,
    codesign_failures,
    pin_runner_environment,
    runner_store_paths,
    signed_mach_o,
    verify_closure,
)

DRV = "/nix/store/" + "d" * 32 + "-aihc.drv"


class FakeNix:
    """Answers the commands ``build_compiler`` runs, against a fake store directory.

    ``outcomes`` overrides the answer to a command by its first words, as a
    list consumed one call at a time.
    """

    def __init__(self, store_path: Path, outcomes=None):
        self.store_path = store_path
        self.program = store_path / "bin" / "aihc"
        self.outcomes = outcomes or {}
        self.commands = []

    def __call__(self, command, cwd, timeout, environment=None):
        self.commands.append(command)
        for prefix, answers in self.outcomes.items():
            if tuple(command[: len(prefix)]) == prefix and answers:
                return subprocess.CompletedProcess(command, *answers.pop(0))
        if command[:2] == ["nix", "eval"]:
            return self.ok(json.dumps({"program": str(self.program), "derivations": [DRV]}))
        if command[:2] == ["nix", "build"]:
            self.program.parent.mkdir(parents=True, exist_ok=True)
            self.program.write_text("#!/bin/sh\n")
            return self.ok(f"{self.store_path}\n")
        if command[:2] == ["nix", "path-info"]:
            return self.ok(f"{self.store_path}\n")
        return self.ok("")

    def ok(self, stdout):
        return subprocess.CompletedProcess([], 0, stdout, "")

    def ran(self, *prefix):
        return [command for command in self.commands if tuple(command[: len(prefix)]) == prefix]


class BuildCompilerTests(unittest.TestCase):
    def build(self, fake, directory):
        with patch("aihc_bench.toolchain.run_command", side_effect=fake):
            return build_compiler(Path("/wt"), "aarch64-darwin", Path(directory) / "gcroots" / "aihc", 30)

    def test_the_compiler_is_built_rooted_checked_and_started_once(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeNix(Path(directory) / "store-aihc")
            compiler = self.build(fake, directory)
        self.assertEqual(compiler.program, fake.program)
        self.assertEqual(compiler.store_path, fake.store_path)
        (evaluate,) = fake.ran("nix", "eval")
        self.assertEqual(evaluate[3], "/wt#apps.aarch64-darwin.aihc")
        (build,) = fake.ran("nix", "build")
        # Built and rooted in one step, so the compiler is never in the store
        # without a root -- the window in which worker-m1's was collected.
        self.assertEqual(build[build.index("--out-link") + 1], str(Path(directory) / "gcroots" / "aihc"))
        self.assertEqual(build[-1], f"{DRV}^out")
        self.assertEqual(len(fake.ran("nix", "store", "verify")), 1)
        self.assertEqual(fake.commands[-1], [str(fake.program), "--help"])
        # Nothing runs the compiler through nix any more.
        self.assertEqual(fake.ran("nix", "run"), [])

    def test_a_commit_that_does_not_evaluate_is_the_commits_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeNix(Path(directory) / "s", {("nix", "eval"): [(1, "", "error: attribute 'aihc' missing")]})
            with self.assertRaises(CompilerDoesNotBuild) as raised:
                self.build(fake, directory)
        self.assertIn("attribute 'aihc' missing", raised.exception.detail)

    def test_a_build_that_fails_for_the_machine_is_a_machine_fault(self):
        for output in (
            "error: unable to download 'https://cache.nixos.org/x.narinfo'",
            "error: writing to file: No space left on device",
            "error: builder for '/nix/store/x-aihc.drv' failed due to signal 9 (Killed)",
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                fake = FakeNix(Path(directory) / "s", {("nix", "build"): [(1, "", output)]})
                with self.assertRaises(MachineFault):
                    self.build(fake, directory)

    def test_a_compiler_killed_at_start_is_a_machine_fault(self):
        """worker-m1's compiler: macOS killed it for a bad code signature page."""
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "s"
            fake = FakeNix(store, {(str(store / "bin" / "aihc"), "--help"): [(-9, "", "")]})
            with self.assertRaises(MachineFault) as raised:
                self.build(fake, directory)
        self.assertIn("signal 9", str(raised.exception))

    def test_a_compiler_that_starts_and_fails_is_the_commits_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "s"
            fake = FakeNix(store, {(str(store / "bin" / "aihc"), "--help"): [(1, "", "aihc: panic")]})
            with self.assertRaises(CompilerDoesNotBuild):
                self.build(fake, directory)

    def test_a_build_that_fails_verification_is_deleted_and_built_again(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "s"
            modified = "/nix/store/" + "b" * 32 + "-aihc-0.1.0.0"
            fake = FakeNix(store, {("nix", "store", "verify"): [(1, "", f"path '{modified}' was modified! expected hash")]})
            compiler = self.build(fake, directory)
        self.assertEqual(compiler.store_path, store)
        (delete,) = fake.ran("nix", "store", "delete")
        self.assertEqual(sorted(delete[3:]), sorted([str(store), modified]))
        self.assertEqual(len(fake.ran("nix", "build")), 2)

    def test_a_build_that_fails_verification_twice_is_a_machine_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeNix(
                Path(directory) / "s",
                {("nix", "store", "verify"): [(1, "", "path '/nix/store/" + "b" * 32 + "-x' was modified!")] * 2},
            )
            with self.assertRaises(MachineFault) as raised:
                self.build(fake, directory)
        self.assertIn("nix store delete", str(raised.exception))
        self.assertEqual(len(fake.ran("nix", "build")), 2)

    def test_a_bad_build_held_by_something_else_is_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeNix(
                Path(directory) / "s",
                {
                    ("nix", "store", "verify"): [(1, "", "path '/nix/store/" + "b" * 32 + "-x' was modified!")],
                    ("nix", "store", "delete"): [(1, "", "error: cannot delete path because it is still alive")],
                },
            )
            with self.assertRaises(MachineFault):
                self.build(fake, directory)
        self.assertEqual(len(fake.ran("nix", "build")), 1)


    def test_rebuilding_replaces_only_its_own_root(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = Path(directory) / "gcroots"
            roots.mkdir()
            for name in ("aihc", "aihc-1", "aihc-index"):
                (roots / name).symlink_to("/nix/store/old")
            fake = FakeNix(Path(directory) / "s")
            self.build(fake, directory)
            self.assertFalse((roots / "aihc-1").is_symlink())
            self.assertTrue((roots / "aihc-index").is_symlink())


class VerificationTests(unittest.TestCase):
    def test_a_sound_closure_has_no_problems(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeNix(Path(directory))
            with patch("aihc_bench.toolchain.run_command", side_effect=fake):
                self.assertEqual(verify_closure([Path(directory)], 30), {})

    def test_mach_o_executables_and_libraries_are_recognised(self):
        def header(magic, file_type):
            return magic + struct.pack("<III", 0x0100000C, 0, file_type)

        with tempfile.TemporaryDirectory() as directory:
            cases = {
                "executable": (header(b"\xcf\xfa\xed\xfe", 2), True),
                "dylib": (header(b"\xcf\xfa\xed\xfe", 6), True),
                "object": (header(b"\xcf\xfa\xed\xfe", 1), False),
                "script": (b"#!/bin/sh\necho hello\n", False),
                "short": (b"\xcf\xfa", False),
            }
            for name, (contents, expected) in cases.items():
                path = Path(directory) / name
                path.write_bytes(contents)
                self.assertEqual(signed_mach_o(path), expected, name)

    def test_codesign_names_each_file_that_fails(self):
        stderr = "/a/aihc: invalid signature (code or signature have been modified)\nIn architecture: arm64\n"
        with patch("aihc_bench.toolchain.run_command", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
            failures = codesign_failures(["/a/aihc", "/a/other"], 30)
        self.assertEqual(failures, {"/a/aihc": "invalid signature (code or signature have been modified)"})

    def test_a_codesign_failure_it_cannot_attribute_still_fails(self):
        with patch("aihc_bench.toolchain.run_command", return_value=subprocess.CompletedProcess([], 1, "", "odd")):
            self.assertEqual(codesign_failures(["/a/aihc"], 30), {"/a/aihc": "odd"})


class RunnerEnvironmentTests(unittest.TestCase):
    def test_store_paths_come_from_path_and_the_suites_variables(self):
        toolchains = "/nix/store/" + "a" * 32 + "-aihc-bench-toolchains"
        clang = "/nix/store/" + "c" * 32 + "-clang"
        python = "/nix/store/" + "p" * 32 + "-python3"
        environment = {
            "PATH": f"{clang}/bin:/usr/bin:{toolchains}/bin",
            "AIHC_BENCH_TOOLCHAINS": toolchains,
            "HOME": "/nix/store/" + "h" * 32 + "-not-a-tool",
        }
        paths = runner_store_paths(environment, executable=f"{python}/bin/python3")
        self.assertEqual(paths, sorted(Path(path) for path in (toolchains, clang, python)))

    def test_the_runners_tools_are_rooted_and_a_failure_is_the_machines(self):
        tool = Path("/nix/store/" + "a" * 32 + "-tools")
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeNix(Path(directory), {("nix", "build"): [(1, "", "error: path is not valid")]})
            with (
                patch("aihc_bench.toolchain.runner_store_paths", return_value=[tool]),
                patch("aihc_bench.toolchain.run_command", side_effect=fake),
                self.assertRaises(MachineFault),
            ):
                pin_runner_environment(Path(directory), 30)
            (build,) = fake.ran("nix", "build")
            self.assertEqual(build[build.index("--out-link") + 1], str(Path(directory) / ".cache" / "gcroots" / "runner" / "path"))
            self.assertEqual(build[-1], str(tool))


if __name__ == "__main__":
    unittest.main()
