#!/usr/bin/env python3
"""Compile a benchmark's self-contained Cabal package with GHC.

Invoked as the ``compile`` command for GHC configurations in
``benchmark.json``. Unlike the old single-file ``ghc`` invocation, this runs
a real ``cabal build`` against the benchmark's own ``cabal.project.freeze``,
so dependency resolution and dependency compilation are included in the
wall-clock time the runner measures around this whole process. That also
means a private ``--store-dir`` per configuration: sharing the user's store
would let the first configuration pay for the dependencies and the rest
measure only the benchmark.

A generated, out-of-tree project file (rather than editing the benchmark's
own ``cabal.project``) keeps the checked-in benchmark directory untouched and
lets multiple configurations build the same benchmark in parallel without
racing on a shared file.  It also carries the profile's optimization level:
cabal's command-line ``-O`` flag reaches local packages only, so the Hackage
dependencies -- where most of a benchmark's work lives -- were configured
``--enable-optimization`` (``-O1``) in every profile while only the benchmark
itself followed the profile.  A ``package *`` stanza is what applies a level
to the whole build plan, matching AIHC, which passes its ``-O`` to everything
it compiles.

The freeze file is written by ``cabal freeze`` under one particular GHC, so
it also pins that GHC's boot libraries (``base``, ``bytestring``, ...). Every
other GHC ships different versions of those, and the Wasm cross-compiler
ships its own again, so the generated project only carries the freeze
constraints for packages the chosen GHC does not already provide: the
Hackage dependencies stay pinned to the same version for every toolchain
while the boot library versions come from the compiler under test.

Those boot libraries are then rebuilt from source rather than taken as GHC
shipped them, at the version the compiler ships and at the profile's
optimization level (``boot_library_packages``). GHC's are compiled once, at
the level its own release was built with, so an ``O0`` profile otherwise
linked an optimized ``text`` and ``containers`` while AIHC compiled its
equivalents at ``-O0``. ``base`` and the rest of ``WIRED_IN_PACKAGES`` cannot
move. The rebuild happens in ``--prepare``, outside the timed compile, for
the same reason AIHC's boot equivalents are installed outside it.

The toolchain's ``ghc-pkg`` and ``hsc2hs`` are always passed explicitly, and
the same tools are also put on ``PATH`` under their bare names. Cabal
otherwise guesses them from the ``ghc`` path and falls back to whatever is on
``PATH``, which is either nothing (a clean Nix environment) or a different
GHC's tools, and either way the build fails; the explicit flags do not cover
every lookup cabal makes, which is how a cross build reached
``[Cabal-7620] The program 'ghc-pkg' is required but it could not be found``
while ``--with-hc-pkg`` was being passed. ``cabal list-bin`` is
given exactly the flags ``cabal build`` was, because cabal treats any
difference as a new configuration and resolves the toolchain again.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from aihc_bench.freeze import parse_build_depends, parse_freeze  # noqa: E402

#: Run GHC itself with every core available.  ``-N`` alone needs the threaded
#: RTS, which every GHC the suite measures is built with.
RTS_OPTIONS = "+RTS -N -RTS"

# The cabal -O level for each benchmark profile. GHC has no size-oriented
# level, so the Os profile records what GHC produces at -O1.
GHC_LEVELS = {"O0": "O0", "O1": "O1", "O2": "O2", "Os": "O1"}

#: Packages wired into the compiler: ``base`` and friends are tied to the GHC
#: that ships them and cannot be rebuilt from Hackage against it, so a
#: ``source`` constraint on one is unsatisfiable. Whatever they depend on has
#: to stay installed too (``template-haskell`` reaches ``ghc-boot-th``,
#: ``pretty`` and ``deepseq``), because one package cannot have an installed
#: and a source instance in the same plan.
WIRED_IN_PACKAGES = frozenset(
    {"base", "ghc-bignum", "ghc-internal", "ghc-prim", "integer-gmp", "rts", "system-cxx-std-lib", "template-haskell"}
)

#: Shipped by GHC but not on Hackage as the compiler builds them; nothing a
#: benchmark depends on reaches these, and they are excluded by name rather
#: than by closure so that the ``ghc`` library -- which depends on half the
#: boot set -- does not drag ``text`` and ``bytestring`` back with it.
COMPILER_PRIVATE_PACKAGES = frozenset(
    {"ghc", "ghc-boot", "ghc-boot-th", "ghc-compact", "ghc-experimental", "ghc-heap", "ghc-platform", "ghc-toolchain", "ghci"}
)

#: The synthetic package whose only content is a dependency on the boot
#: libraries a benchmark uses; building it fills the store ahead of the
#: timed compile.
BOOT_PACKAGE = "aihc-bench-boot"

_INDEX_STATE = re.compile(r"(?m)^index-state:\s*(.+?)\s*$")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="benchmark package directory")
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--exe", required=True, help="cabal executable component name")
    parser.add_argument("--ghc", required=True, help="path to the pinned ghc binary")
    parser.add_argument("--ghc-pkg", help="path to that GHC's ghc-pkg (default: ghc-pkg-<suffix> next to --ghc)")
    parser.add_argument("--hsc2hs", help="path to that GHC's hsc2hs (default: hsc2hs-<suffix> next to --ghc)")
    parser.add_argument("--cabal", default="cabal", help="cabal-install binary (default: cabal on PATH)")
    parser.add_argument("--optimization", required=True, choices=["O0", "O1", "O2", "Os"], help="benchmark profile; GHC has no size level, so Os builds with -O1")
    parser.add_argument("--ghc-option", action="append", default=[], help="extra -f.../-rtsopts flag, repeatable")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="build the benchmark's boot libraries into the store and archive it, instead of compiling the benchmark",
    )
    args = parser.parse_args(argv)
    args.source = args.source.resolve()
    args.build_dir = args.build_dir.resolve()
    args.artifact = args.artifact.resolve()
    args.ghc = Path(args.ghc)
    args.ghc_pkg = Path(args.ghc_pkg) if args.ghc_pkg else sibling_tool(args.ghc, "ghc-pkg")
    args.hsc2hs = Path(args.hsc2hs) if args.hsc2hs else sibling_tool(args.ghc, "hsc2hs")
    args.ghc_level = GHC_LEVELS[args.optimization]
    return args


def sibling_tool(ghc: Path, tool: str) -> Path:
    """``.../ghc-9.14.1`` -> ``.../ghc-pkg-9.14.1``; the toolchain wrappers share the ghc's suffix."""
    return ghc.with_name(tool + ghc.name[len("ghc") :])


def installed_packages(ghc_pkg: Path) -> set[str]:
    """Names of the packages the compiler ships in its global database."""
    return set(global_packages(ghc_pkg))


@functools.lru_cache(maxsize=None)
def global_packages(ghc_pkg: Path) -> dict[str, tuple[str, list[str]]]:
    """``name -> (version, dependency names)`` for the compiler's global database.

    Cached: the project file is generated inside the timed compile, and
    ``compile_time`` should not carry an interrogation of the compiler for
    every caller that needs the shipped set.
    """
    dump = subprocess.run([str(ghc_pkg), "dump", "--global"], capture_output=True, text=True, check=True).stdout
    blocks = []
    for block in dump.split("\n---\n"):
        name = version = unit_id = None
        depends: list[str] = []
        field = None
        for line in block.splitlines():
            if line[:1] in (" ", "\t"):
                if field == "depends":
                    depends.extend(line.split())
                continue
            if ":" not in line:
                continue
            field, _, value = line.partition(":")
            value = value.strip()
            if field == "name":
                name = value
            elif field == "version":
                version = value
            elif field == "id":
                unit_id = value
            elif field == "depends" and value:
                depends.extend(value.split())
        if name and version:
            blocks.append((name, version, unit_id, depends))
    names_by_unit = {unit_id: name for name, _, unit_id, _ in blocks if unit_id}
    packages: dict[str, tuple[str, list[str]]] = {}
    for name, version, _, depends in blocks:
        resolved = [names_by_unit[unit] for unit in depends if unit in names_by_unit]
        packages[name] = (version, resolved)
    return packages


def compiler_bound_packages(packages: dict[str, tuple[str, list[str]]]) -> set[str]:
    """The shipped packages that have to keep coming from the compiler.

    The wired-in ones and everything they depend on, plus the ones GHC builds
    privately. Everything else the compiler ships is an ordinary Hackage
    package that a benchmark can compile itself.
    """
    bound = set()
    pending = [name for name in WIRED_IN_PACKAGES if name in packages]
    while pending:
        name = pending.pop()
        if name in bound:
            continue
        bound.add(name)
        pending.extend(dependency for dependency in packages[name][1] if dependency not in bound)
    return bound | (COMPILER_PRIVATE_PACKAGES & set(packages))


def boot_library_packages(ghc_pkg: Path) -> dict[str, str]:
    """``name -> version`` for the boot libraries a benchmark rebuilds itself.

    GHC ships these compiled at the level its own release was built with, so
    an ``O0`` profile linked an optimized ``text`` and ``containers`` while
    AIHC compiled its equivalents at the profile's level (see
    ``_prepare_aihc_store``). Rebuilding them from source puts both compilers
    on the same footing. ``base``, ``ghc-prim`` and the rest of
    ``WIRED_IN_PACKAGES`` cannot move, so a benchmark that only uses ``base``
    is unaffected.
    """
    packages = global_packages(ghc_pkg)
    bound = compiler_bound_packages(packages)
    return {name: version for name, (version, _) in packages.items() if name not in bound}


def boot_constraints(boot_libraries: dict[str, str]) -> list[str]:
    """Force each boot library to be built from source, at the shipped version.

    The version pin keeps the experiment to one variable: the solver otherwise
    picks whatever the index-state offers -- it chose ``containers-0.8`` over
    the ``0.7`` GHC 9.12.4 ships -- and the profile comparison would then also
    be comparing library releases. A constraint on a package no benchmark
    reaches never enters a plan and costs nothing.
    """
    constraints = []
    for name in sorted(boot_libraries):
        constraints.append(f"any.{name} source")
        constraints.append(f"any.{name} =={boot_libraries[name]}")
    return constraints


def boot_allow_newer(boot_libraries: dict[str, str]) -> str:
    """Let the boot libraries build against the compiler's own core packages.

    GHC 9.14.1 ships ``base-4.22.1.0`` and ``array-0.5.8.0``, but the
    ``array-0.5.8.0`` release on Hackage caps ``base < 4.22``: the compiler
    ships that source with its bounds bumped to match the ``base`` it also
    ships, and the release cannot have anticipated it. Building the library
    from source means taking the bounds from the release, so a rebuild of
    ``array`` is rejected outright without this.

    Relaxing bounds cannot change which versions are used, because every boot
    library is pinned to the version the compiler ships; it only stops a stale
    bound from rejecting a version that is already decided. The relaxation is
    scoped to the boot libraries, so the benchmark's own Hackage dependencies
    still have their bounds enforced.
    """
    return ", ".join(f"{name}:*" for name in sorted(boot_libraries))


def project_constraints(freeze_file: Path, provided: set[str]) -> list[str]:
    """Freeze constraints for the packages the compiler does not provide itself."""
    pinned = parse_freeze(freeze_file)
    return [f"any.{name} =={version}" for name, version in sorted(pinned.items()) if name not in provided]


def package_stanza(ghc_level: str, ghc_options: list[str]) -> str:
    """The profile's ``-O`` level and GHC flags, applied to every package.

    Cabal's command-line ``-O`` and ``--ghc-options`` configure local packages
    only. Each dependency was handed ``--enable-optimization`` (``-O1``)
    whatever the profile said, and none of them ever saw ``-fllvm``, so a
    benchmark whose work sits in a Hackage dependency (``snappy-hs``,
    ``aihc-cpp``) was measured at ``-O1`` through the native backend in every
    profile and configuration. A ``package *`` stanza is per-package
    configuration and does reach dependencies; it is part of their unit id,
    so each profile and backend gets its own store entry rather than reusing
    another's.
    """
    options = " ".join([*ghc_options, RTS_OPTIONS])
    return f"package *\n  optimization: {ghc_level[1:]}\n  ghc-options: {options}\n"


def project_body(args: argparse.Namespace) -> str:
    """Everything below ``packages:``, identical for the benchmark and the
    boot package.

    The two builds have to agree exactly: a store entry is reused only when
    its unit id matches, and the constraints and the ``package *`` stanza are
    part of that hash. A boot library prepared under a different body would
    be rebuilt inside the timed compile, which is the cost this whole step
    exists to avoid.
    """
    freeze_file = args.source / "cabal.project.freeze"
    contents = []
    boot_libraries = boot_library_packages(args.ghc_pkg)
    constraints = boot_constraints(boot_libraries)
    index_state = None
    if freeze_file.is_file():
        constraints += project_constraints(freeze_file, installed_packages(args.ghc_pkg))
        index_state = _INDEX_STATE.search(freeze_file.read_text(encoding="utf-8"))
    if constraints:
        contents.append("constraints: " + ",\n             ".join(constraints) + "\n")
    if boot_libraries:
        contents.append(f"allow-newer: {boot_allow_newer(boot_libraries)}\n")
    if index_state:
        contents.append(f"index-state: {index_state.group(1)}\n")
    # Last: a ``package`` stanza is indentation-delimited, so anything written
    # after it would have to stay unindented to remain a top-level field.
    contents.append(package_stanza(args.ghc_level, args.ghc_option))
    return "".join(contents)


def generate_project_file(args: argparse.Namespace) -> Path:
    args.build_dir.mkdir(parents=True, exist_ok=True)
    project_file = args.build_dir / "cabal.project"
    project_file.write_text(f"packages: {args.source}\n" + project_body(args), encoding="utf-8")
    return project_file


def generate_boot_project(args: argparse.Namespace) -> Path:
    """A package that depends on the benchmark's boot libraries and nothing else.

    ``cabal build --only-dependencies`` would also build the benchmark's
    Hackage dependencies, and those belong inside the timed compile: GHC
    should pay for ``snappy-hs`` exactly as AIHC does. Only the libraries GHC
    would otherwise have shipped ready-made are prepared here, which is the
    same line ``_prepare_aihc_store`` draws on the AIHC side.
    """
    boot_directory = args.build_dir / "boot"
    boot_directory.mkdir(parents=True, exist_ok=True)
    depends = boot_dependencies(args)
    (boot_directory / f"{BOOT_PACKAGE}.cabal").write_text(
        "cabal-version: 2.4\n"
        f"name: {BOOT_PACKAGE}\n"
        "version: 0\n"
        "\nlibrary\n"
        f"  build-depends: {', '.join(depends)}\n"
        "  default-language: Haskell2010\n",
        encoding="utf-8",
    )
    project_file = args.build_dir / "boot.project"
    project_file.write_text(f"packages: {boot_directory}\n" + project_body(args), encoding="utf-8")
    return project_file


def boot_dependencies(args: argparse.Namespace) -> list[str]:
    """The benchmark's direct dependencies that GHC ships as boot libraries.

    Their own boot dependencies come along with them, since cabal builds the
    whole plan. A benchmark that only uses ``base`` has none: ``base`` is
    wired into the compiler and stays as GHC shipped it.
    """
    cabal_files = sorted(args.source.glob("*.cabal"))
    if not cabal_files:
        return []
    boot_libraries = boot_library_packages(args.ghc_pkg)
    return [name for name in parse_build_depends(cabal_files[0]) if name in boot_libraries]


def cabal_command(args: argparse.Namespace, project_file: Path, verb: str, target: str | None = None) -> list[str]:
    """``cabal <verb>`` with the full configuration; identical for build and list-bin.

    The GHC flags live in the project file's ``package *`` stanza rather than
    in ``--ghc-options`` here, because the command-line form reaches local
    packages only; see ``package_stanza``.
    """
    return [
        args.cabal,
        # A private store per configuration.  The shared user-wide store hands
        # every configuration after the first its dependencies already
        # compiled, so compile time would describe the benchmark alone for
        # some configurations and the benchmark plus its dependencies for
        # others; concurrent configurations also raced to create the shared
        # store's package db ("cannot create: ... package.db already exists").
        # This is a global flag, so it precedes the verb.
        f"--store-dir={store_directory(args.build_dir)}",
        verb,
        f"--project-file={project_file}",
        f"--builddir={args.build_dir / ('boot-dist' if target else 'dist')}",
        f"--with-compiler={args.ghc}",
        f"--with-hc-pkg={args.ghc_pkg}",
        f"--with-hsc2hs={args.hsc2hs}",
        f"-{args.ghc_level}",
        target or f"exe:{args.exe}",
    ]


def store_directory(build_dir: Path) -> Path:
    """Where the store sits; a cabal store records absolute paths and cannot move."""
    return build_dir / "store"


def prepare_boot_store(args: argparse.Namespace, environment: dict) -> int:
    """Build the benchmark's boot libraries into the store, then archive it.

    Runs outside the timed compile, mirroring the AIHC store preparation, so
    that rebuilding ``text`` from source does not land on ``compile_time``:
    GHC hands these libraries over ready-made, and the suite only rebuilds
    them to give them the profile's optimization level.

    The archive is what makes that affordable. A cabal store bakes its own
    absolute path into the package database, so a prepared store cannot be
    copied to another directory -- it is restored, over and over, to the very
    path it was built at, once per timed compile.
    """
    depends = boot_dependencies(args)
    archive = boot_archive(args.build_dir)
    if not depends:
        # Nothing to prepare: an empty archive still tells the runner the
        # preparation ran, and restoring it leaves an empty store.
        archive.parent.mkdir(parents=True, exist_ok=True)
        tarfile.open(archive, "w").close()
        return 0
    project_file = generate_boot_project(args)
    build = subprocess.run(
        cabal_command(args, project_file, "build", target=f"lib:{BOOT_PACKAGE}"),
        capture_output=True,
        text=True,
        env=environment,
    )
    if build.returncode != 0:
        sys.stderr.write(build.stdout)
        sys.stderr.write(build.stderr)
        return build.returncode
    store = store_directory(args.build_dir)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w") as tar:
        tar.add(store, arcname=".")
    return 0


def boot_archive(build_dir: Path) -> Path:
    """The prepared store's archive, kept beside the build directory.

    Every timed compile starts by deleting the build directory, so the
    archive cannot live inside it.
    """
    return build_dir.parent / "boot-store.tar"


def restore_boot_store(build_dir: Path) -> None:
    """Put the prepared boot libraries back, at the path they were built at.

    Called before each timed compile. What the previous compile added to the
    store -- the benchmark's own Hackage dependencies -- is gone with the
    build directory, so every compile sees the same store: the boot libraries
    GHC would have shipped, and nothing else.
    """
    archive = boot_archive(build_dir)
    if not archive.is_file():
        return
    store = store_directory(build_dir)
    store.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r") as tar:
        # This suite wrote the archive from a store it had just built; the
        # extraction filters exist for archives from elsewhere, and the
        # default becomes a restrictive one in Python 3.14.
        if sys.version_info >= (3, 12):
            tar.extractall(store, filter="fully_trusted")
        else:
            tar.extractall(store)


def toolchain_path(args: argparse.Namespace) -> Path:
    """A directory of bare-named links to this configuration's toolchain.

    ``--with-compiler``/``--with-hc-pkg``/``--with-hsc2hs`` cover most of what
    cabal runs, but not all of it, and an uncovered lookup falls back to the
    bare name on ``PATH``.  Pointing those bare names at the very tools the
    flags name keeps the fallback on the same toolchain instead of failing or,
    worse, silently using a different GHC's ``ghc-pkg``.
    """
    directory = args.build_dir / "toolchain"
    directory.mkdir(parents=True, exist_ok=True)
    for name, tool in (("ghc", args.ghc), ("ghc-pkg", args.ghc_pkg), ("hsc2hs", args.hsc2hs)):
        link = directory / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(tool)
    return directory


def cabal_environment(args: argparse.Namespace) -> dict:
    """The environment cabal runs under, with the toolchain first on ``PATH``."""
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join([str(toolchain_path(args)), environment.get("PATH", "")])
    return environment


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    for name, tool in (("ghc-pkg", args.ghc_pkg), ("hsc2hs", args.hsc2hs)):
        if not tool.is_file():
            sys.stderr.write(f"{name} for {args.ghc} not found at {tool}; pass --{name}\n")
            return 1
    environment = cabal_environment(args)
    if args.prepare:
        return prepare_boot_store(args, environment)
    project_file = generate_project_file(args)
    build = subprocess.run(
        cabal_command(args, project_file, "build"), capture_output=True, text=True, env=environment
    )
    if build.returncode != 0:
        sys.stderr.write(build.stdout)
        sys.stderr.write(build.stderr)
        return build.returncode

    located = subprocess.run(
        cabal_command(args, project_file, "list-bin"), capture_output=True, text=True, env=environment
    )
    if located.returncode != 0:
        sys.stderr.write(located.stdout)
        sys.stderr.write(located.stderr)
        return located.returncode
    binary = Path(located.stdout.strip().splitlines()[-1])
    if not binary.is_file():
        sys.stderr.write(f"cabal list-bin reported a binary that does not exist: {binary}\n")
        return 1

    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, args.artifact)
    args.artifact.chmod(0o755)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
