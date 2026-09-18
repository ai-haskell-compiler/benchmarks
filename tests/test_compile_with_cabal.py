import os
import stat
import tempfile
import unittest
from pathlib import Path

from aihc_bench.scripts.compile_with_cabal import (
    RTS_OPTIONS,
    cabal_command,
    cabal_environment,
    generate_project_file,
    optimization_stanza,
    parse_args,
    project_constraints,
    sibling_tool,
    toolchain_path,
)

FREEZE = (
    "active-repositories: hackage.haskell.org:merge\n"
    "constraints: any.base ==4.21.2.0,\n"
    "             any.bytestring ==0.12.2.0,\n"
    "             any.snappy-hs ==0.1.2.0\n"
    "index-state: hackage.haskell.org 2026-09-10T11:00:24Z\n"
)


def fake_ghc_pkg(directory: Path, name: str, packages: str) -> Path:
    tool = directory / name
    tool.write_text(f"#!/bin/sh\necho '{packages}'\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


class CompileWithCabalTests(unittest.TestCase):
    def test_sibling_tools_share_the_ghc_suffix(self):
        self.assertEqual(sibling_tool(Path("/t/bin/ghc-9.14.1"), "ghc-pkg"), Path("/t/bin/ghc-pkg-9.14.1"))
        self.assertEqual(sibling_tool(Path("/t/bin/ghc-9.14.1-wasm"), "hsc2hs"), Path("/t/bin/hsc2hs-9.14.1-wasm"))
        self.assertEqual(sibling_tool(Path("/t/bin/ghc"), "ghc-pkg"), Path("/t/bin/ghc-pkg"))

    def test_boot_libraries_are_left_to_the_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            freeze = Path(directory) / "cabal.project.freeze"
            freeze.write_text(FREEZE, encoding="utf-8")
            # GHC 9.14 ships base-4.22, so the 9.12 pin must not reach it; the
            # Hackage dependency stays pinned for every toolchain.
            self.assertEqual(project_constraints(freeze, {"base", "bytestring", "rts"}), ["any.snappy-hs ==0.1.2.0"])
            self.assertEqual(project_constraints(freeze, set()), ["any.base ==4.21.2.0", "any.bytestring ==0.12.2.0", "any.snappy-hs ==0.1.2.0"])

    def test_generated_project_pins_only_what_ghc_lacks_and_keeps_the_index_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pkg"
            source.mkdir()
            (source / "cabal.project.freeze").write_text(FREEZE, encoding="utf-8")
            fake_ghc_pkg(root, "ghc-pkg-9.14.1", "base bytestring ghc-prim")
            args = parse_args(["--source", str(source), "--build-dir", str(root / "build"), "--artifact", str(root / "out"), "--exe", "pkg", "--ghc", str(root / "ghc-9.14.1"), "--optimization", "Os"])
            project = generate_project_file(args)
            self.assertEqual(
                project.read_text(encoding="utf-8"),
                f"packages: {source.resolve()}\n"
                "constraints: any.snappy-hs ==0.1.2.0\n"
                "index-state: hackage.haskell.org 2026-09-10T11:00:24Z\n"
                "package *\n  optimization: 1\n",
            )

    def test_the_profile_level_reaches_every_package(self):
        """cabal's command-line -O covers local packages only.

        Its dependencies are configured --enable-optimization (-O1) whatever
        the command line said, so snappy-hs and aihc-cpp -- where most of a
        benchmark's work is -- were measured at -O1 in all four profiles. A
        ``package *`` stanza is per-package configuration and does reach them.
        """
        self.assertEqual(optimization_stanza("O0"), "package *\n  optimization: 0\n")
        self.assertEqual(optimization_stanza("O2"), "package *\n  optimization: 2\n")

    def test_the_stanza_closes_the_generated_project(self):
        """A ``package`` stanza swallows the indented lines after it, and the
        freeze constraints are written as an indented continuation list."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pkg"
            source.mkdir()
            (source / "cabal.project.freeze").write_text(FREEZE, encoding="utf-8")
            fake_ghc_pkg(root, "ghc-pkg-9.14.1", "base ghc-prim")
            args = parse_args(["--source", str(source), "--build-dir", str(root / "build"), "--artifact", str(root / "out"), "--exe", "pkg", "--ghc", str(root / "ghc-9.14.1"), "--optimization", "O0"])
            lines = generate_project_file(args).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[-2:], ["package *", "  optimization: 0"])

    def test_list_bin_repeats_the_build_configuration(self):
        args = parse_args(["--source", "/src", "--build-dir", "/build", "--artifact", "/out", "--exe", "pkg", "--ghc", "/t/bin/ghc-9.14.1-wasm", "--optimization", "Os", "--ghc-option=-rtsopts"])
        build = cabal_command(args, Path("/build/cabal.project"), "build")
        located = cabal_command(args, Path("/build/cabal.project"), "list-bin")
        # cabal treats any flag difference as a new configuration and resolves
        # the toolchain again, so the two invocations must match exactly.
        self.assertEqual(build[3:], located[3:])
        self.assertEqual([build[0], build[2]], ["cabal", "build"])
        self.assertEqual([located[0], located[2]], ["cabal", "list-bin"])
        self.assertIn("--with-compiler=/t/bin/ghc-9.14.1-wasm", build)
        self.assertIn("--with-hc-pkg=/t/bin/ghc-pkg-9.14.1-wasm", build)
        self.assertIn("--with-hsc2hs=/t/bin/hsc2hs-9.14.1-wasm", build)
        self.assertIn("-O1", build)  # GHC has no size level
        self.assertIn("--ghc-options=-rtsopts", build)
        self.assertEqual(build[-1], "exe:pkg")

    def test_each_configuration_builds_in_its_own_store(self):
        """A shared store would give every configuration after the first its
        dependencies pre-compiled, and racing cabals fight over its package db."""
        args = parse_args(["--source", "/src", "--build-dir", "/build", "--artifact", "/out", "--exe", "pkg", "--ghc", "/t/bin/ghc-9.14.1", "--optimization", "O2"])
        build = cabal_command(args, Path("/build/cabal.project"), "build")
        self.assertIn("--builddir=/build/dist", build)
        # --store-dir is a global flag: after the verb cabal rejects it with
        # "unrecognized 'build' option".
        self.assertEqual(build[1], "--store-dir=/build/store")
        self.assertEqual(build[2], "build")

    def test_ghc_is_given_the_whole_machine(self):
        """Compile time is measured multithreaded, so GHC itself runs with -N."""
        args = parse_args(["--source", "/src", "--build-dir", "/build", "--artifact", "/out", "--exe", "pkg", "--ghc", "/t/bin/ghc-9.14.1", "--optimization", "O2"])
        build = cabal_command(args, Path("/build/cabal.project"), "build")
        self.assertIn(f"--ghc-options={RTS_OPTIONS}", build)
        self.assertEqual(RTS_OPTIONS, "+RTS -N -RTS")

    def test_the_toolchain_is_on_path_under_bare_names(self):
        """Cabal falls back to bare names for lookups --with-* does not cover.

        A cross build hit [Cabal-7620] for 'ghc-pkg' while --with-hc-pkg was
        being passed, so the same tools are also reachable by bare name.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            toolchain = root / "bin"
            toolchain.mkdir()
            for name in ("ghc-9.14.1-wasm", "ghc-pkg-9.14.1-wasm", "hsc2hs-9.14.1-wasm"):
                (toolchain / name).write_text("#!/bin/sh\n", encoding="utf-8")
            args = parse_args(["--source", "/src", "--build-dir", str(root / "build"), "--artifact", "/out", "--exe", "pkg", "--ghc", str(toolchain / "ghc-9.14.1-wasm"), "--optimization", "O2"])
            links = toolchain_path(args)
            self.assertEqual(sorted(p.name for p in links.iterdir()), ["ghc", "ghc-pkg", "hsc2hs"])
            self.assertEqual((links / "ghc-pkg").resolve(), (toolchain / "ghc-pkg-9.14.1-wasm").resolve())
            environment = cabal_environment(args)
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(links))

    def test_the_toolchain_links_survive_a_rebuild(self):
        """The build directory is reused across configurations of a run."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            toolchain = root / "bin"
            toolchain.mkdir()
            for name in ("ghc-9.14.1", "ghc-pkg-9.14.1", "hsc2hs-9.14.1"):
                (toolchain / name).write_text("#!/bin/sh\n", encoding="utf-8")
            args = parse_args(["--source", "/src", "--build-dir", str(root / "build"), "--artifact", "/out", "--exe", "pkg", "--ghc", str(toolchain / "ghc-9.14.1"), "--optimization", "O2"])
            toolchain_path(args)
            self.assertEqual((toolchain_path(args) / "ghc").resolve(), (toolchain / "ghc-9.14.1").resolve())


if __name__ == "__main__":
    unittest.main()
