import os
import stat
import tempfile
import unittest
from pathlib import Path

from aihc_bench.scripts.compile_with_cabal import (
    RTS_OPTIONS,
    boot_archive,
    boot_allow_newer,
    boot_constraints,
    cabal_command,
    cabal_environment,
    compiler_bound_packages,
    generate_project_file,
    global_packages,
    package_stanza,
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


DUMP = """name: base
version: 4.21.2.0
id: base-4.21.2.0-fc24
depends: ghc-internal-9.1202.0-a1b2 ghc-prim-0.13.0-6940
---
name: ghc-internal
version: 9.1202.0
id: ghc-internal-9.1202.0-a1b2
depends: ghc-prim-0.13.0-6940
---
name: ghc-prim
version: 0.13.0
id: ghc-prim-0.13.0-6940
depends:
---
name: template-haskell
version: 2.23.0.0
id: template-haskell-2.23.0.0-0054
depends: base-4.21.2.0-fc24 ghc-boot-th-9.12.4-9b7a
---
name: ghc-boot-th
version: 9.12.4
id: ghc-boot-th-9.12.4-9b7a
depends: base-4.21.2.0-fc24 pretty-1.1.3.6-1e7a
---
name: pretty
version: 1.1.3.6
id: pretty-1.1.3.6-1e7a
depends: base-4.21.2.0-fc24 deepseq-1.5.1.0-f97a
---
name: deepseq
version: 1.5.1.0
id: deepseq-1.5.1.0-f97a
depends: base-4.21.2.0-fc24
---
name: ghc
version: 9.12.4
id: ghc-9.12.4-aaaa
depends: base-4.21.2.0-fc24 text-2.1.4-c193 bytestring-0.12.2.0-53dd
---
name: text
version: 2.1.4
id: text-2.1.4-c193
depends: base-4.21.2.0-fc24 template-haskell-2.23.0.0-0054 bytestring-0.12.2.0-53dd
---
name: bytestring
version: 0.12.2.0
id: bytestring-0.12.2.0-53dd
depends: base-4.21.2.0-fc24
"""


def fake_ghc_pkg_dump(directory: Path, name: str) -> Path:
    """A ghc-pkg whose 'dump' prints DUMP and whose 'list' prints the names."""
    tool = directory / name
    names = " ".join(sorted({line.split(": ", 1)[1] for line in DUMP.splitlines() if line.startswith("name: ")}))
    tool.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "dump" ]; then\n'
        f"cat <<'END'\n{DUMP}END\n"
        "else\n"
        f"echo '{names}'\n"
        "fi\n",
        encoding="utf-8",
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


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
            fake_ghc_pkg_dump(root, "ghc-pkg-9.14.1")
            args = parse_args(["--source", str(source), "--build-dir", str(root / "build"), "--artifact", str(root / "out"), "--exe", "pkg", "--ghc", str(root / "ghc-9.14.1"), "--optimization", "Os"])
            project = generate_project_file(args)
            self.assertEqual(
                project.read_text(encoding="utf-8"),
                f"packages: {source.resolve()}\n"
                "constraints: any.bytestring source,\n"
                "             any.bytestring ==0.12.2.0,\n"
                "             any.text source,\n"
                "             any.text ==2.1.4,\n"
                "             any.snappy-hs ==0.1.2.0\n"
                "allow-newer: bytestring:*, text:*\n"
                "index-state: hackage.haskell.org 2026-09-10T11:00:24Z\n"
                f"package *\n  optimization: 1\n  ghc-options: {RTS_OPTIONS}\n",
            )

    def test_only_the_compiler_bound_packages_stay_installed(self):
        """base and friends cannot be rebuilt against the GHC that ships them,
        and neither can anything they depend on: template-haskell reaches
        ghc-boot-th, pretty and deepseq, so those stay installed too. The ghc
        library is excluded by name -- taking its closure would drag text and
        bytestring back with it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = fake_ghc_pkg_dump(root, "ghc-pkg-9.12.4")
            packages = global_packages(tool)
            self.assertEqual(packages["text"], ("2.1.4", ["base", "template-haskell", "bytestring"]))
            self.assertEqual(
                sorted(compiler_bound_packages(packages)),
                ["base", "deepseq", "ghc", "ghc-boot-th", "ghc-internal", "ghc-prim", "pretty", "template-haskell"],
            )

    def test_boot_libraries_are_rebuilt_at_the_version_ghc_ships(self):
        """Without the version pin the solver picks whatever the index-state
        offers -- it chose containers-0.8 over the 0.7 GHC 9.12.4 ships -- and
        the profiles would be comparing library releases as well as -O levels."""
        self.assertEqual(
            boot_constraints({"text": "2.1.4", "bytestring": "0.12.2.0"}),
            ["any.bytestring source", "any.bytestring ==0.12.2.0", "any.text source", "any.text ==2.1.4"],
        )

    def test_the_generated_project_rebuilds_the_boot_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pkg"
            source.mkdir()
            fake_ghc_pkg_dump(root, "ghc-pkg-9.14.1")
            args = parse_args(["--source", str(source), "--build-dir", str(root / "build"), "--artifact", str(root / "out"), "--exe", "pkg", "--ghc", str(root / "ghc-9.14.1"), "--optimization", "O0"])
            project = generate_project_file(args).read_text(encoding="utf-8")
            self.assertIn("any.text source", project)
            self.assertIn("any.text ==2.1.4", project)
            self.assertIn("any.bytestring source", project)
            # base is wired into the compiler; a source constraint on it is
            # unsatisfiable, so a base-only benchmark is left as GHC shipped it.
            self.assertNotIn("any.base source", project)
            self.assertNotIn("any.deepseq source", project)
            self.assertNotIn("any.template-haskell source", project)

    def test_a_boot_library_may_predate_the_base_it_is_rebuilt_against(self):
        """GHC 9.14.1 ships base-4.22.1.0 and array-0.5.8.0, but the
        array-0.5.8.0 release on Hackage caps base < 4.22: the compiler ships
        that source with its bounds bumped. Every boot library is pinned to
        the shipped version, so relaxing the bounds cannot change which
        version is used -- and the relaxation stops at the boot libraries, so
        the benchmark's own Hackage dependencies keep theirs."""
        self.assertEqual(boot_allow_newer({"text": "2.1.4", "array": "0.5.8.0"}), "array:*, text:*")

    def test_the_profile_level_reaches_every_package(self):
        """cabal's command-line -O covers local packages only.

        Its dependencies are configured --enable-optimization (-O1) whatever
        the command line said, so snappy-hs and aihc-cpp -- where most of a
        benchmark's work is -- were measured at -O1 in all four profiles. A
        ``package *`` stanza is per-package configuration and does reach them.
        """
        self.assertEqual(package_stanza("O0", []), f"package *\n  optimization: 0\n  ghc-options: {RTS_OPTIONS}\n")
        # -fllvm never reached a dependency either, so the LLVM configurations
        # were compiling snappy-hs and aihc-cpp through the native backend.
        self.assertEqual(
            package_stanza("O2", ["-fllvm", "-rtsopts"]),
            f"package *\n  optimization: 2\n  ghc-options: -fllvm -rtsopts {RTS_OPTIONS}\n",
        )

    def test_the_stanza_closes_the_generated_project(self):
        """A ``package`` stanza swallows the indented lines after it, and the
        freeze constraints are written as an indented continuation list."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pkg"
            source.mkdir()
            (source / "cabal.project.freeze").write_text(FREEZE, encoding="utf-8")
            fake_ghc_pkg_dump(root, "ghc-pkg-9.14.1")
            args = parse_args(["--source", str(source), "--build-dir", str(root / "build"), "--artifact", str(root / "out"), "--exe", "pkg", "--ghc", str(root / "ghc-9.14.1"), "--optimization", "O0"])
            lines = generate_project_file(args).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[-3], "package *")
            self.assertTrue(all(line.startswith("  ") for line in lines[-2:]), lines[-2:])

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
        # The GHC flags live in the project file's stanza, since the
        # command-line form never reaches a dependency.
        self.assertFalse([flag for flag in build if flag.startswith("--ghc-options")], build)
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
        """Compile time is measured multithreaded, so GHC itself runs with -N,
        for every package in the plan rather than the benchmark alone."""
        self.assertEqual(RTS_OPTIONS, "+RTS -N -RTS")
        self.assertTrue(package_stanza("O2", []).endswith(f"  ghc-options: {RTS_OPTIONS}\n"))

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
