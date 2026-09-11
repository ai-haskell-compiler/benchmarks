#!/usr/bin/env python3
"""Compile a benchmark's self-contained Cabal package with GHC.

Invoked as the ``compile`` command for GHC configurations in
``benchmark.json``. Unlike the old single-file ``ghc`` invocation, this runs
a real ``cabal build`` against the benchmark's own ``cabal.project.freeze``,
so dependency resolution and dependency compilation are included in the
wall-clock time the runner measures around this whole process.

A generated, out-of-tree project file (rather than editing the benchmark's
own ``cabal.project``) keeps the checked-in benchmark directory untouched and
lets multiple configurations build the same benchmark in parallel without
racing on a shared file.

The freeze file is written by ``cabal freeze`` under one particular GHC, so
it also pins that GHC's boot libraries (``base``, ``bytestring``, ...). Every
other GHC ships different versions of those, and the Wasm cross-compiler
ships its own again, so the generated project only carries the freeze
constraints for packages the chosen GHC does not already provide: the
Hackage dependencies stay pinned to the same version for every toolchain
while boot libraries come from the compiler under test.

The toolchain's ``ghc-pkg`` and ``hsc2hs`` are always passed explicitly.
Cabal otherwise guesses them from the ``ghc`` path and falls back to whatever
is on ``PATH``, which is either nothing (a clean Nix environment) or a
different GHC's tools, and either way the build fails. ``cabal list-bin`` is
given exactly the flags ``cabal build`` was, because cabal treats any
difference as a new configuration and resolves the toolchain again.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from aihc_bench.freeze import parse_freeze  # noqa: E402

# The cabal -O level for each benchmark profile. GHC has no size-oriented
# level, so the Os profile records what GHC produces at -O1.
GHC_LEVELS = {"O0": "O0", "O1": "O1", "O2": "O2", "Os": "O1"}

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
    listing = subprocess.run([str(ghc_pkg), "list", "--global", "--simple-output", "--names-only"], capture_output=True, text=True, check=True)
    return set(listing.stdout.split())


def project_constraints(freeze_file: Path, provided: set[str]) -> list[str]:
    """Freeze constraints for the packages the compiler does not provide itself."""
    pinned = parse_freeze(freeze_file)
    return [f"any.{name} =={version}" for name, version in sorted(pinned.items()) if name not in provided]


def generate_project_file(args: argparse.Namespace) -> Path:
    args.build_dir.mkdir(parents=True, exist_ok=True)
    project_file = args.build_dir / "cabal.project"
    freeze_file = args.source / "cabal.project.freeze"
    contents = [f"packages: {args.source}\n"]
    if freeze_file.is_file():
        constraints = project_constraints(freeze_file, installed_packages(args.ghc_pkg))
        if constraints:
            contents.append("constraints: " + ",\n             ".join(constraints) + "\n")
        index_state = _INDEX_STATE.search(freeze_file.read_text(encoding="utf-8"))
        if index_state:
            contents.append(f"index-state: {index_state.group(1)}\n")
    project_file.write_text("".join(contents), encoding="utf-8")
    return project_file


def cabal_command(args: argparse.Namespace, project_file: Path, verb: str) -> list[str]:
    """``cabal <verb>`` with the full configuration; identical for build and list-bin."""
    command = [
        args.cabal,
        verb,
        f"--project-file={project_file}",
        f"--builddir={args.build_dir / 'dist'}",
        f"--with-compiler={args.ghc}",
        f"--with-hc-pkg={args.ghc_pkg}",
        f"--with-hsc2hs={args.hsc2hs}",
        f"-{args.ghc_level}",
    ]
    if args.ghc_option:
        command.append("--ghc-options=" + " ".join(args.ghc_option))
    command.append(f"exe:{args.exe}")
    return command


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    for name, tool in (("ghc-pkg", args.ghc_pkg), ("hsc2hs", args.hsc2hs)):
        if not tool.is_file():
            sys.stderr.write(f"{name} for {args.ghc} not found at {tool}; pass --{name}\n")
            return 1
    project_file = generate_project_file(args)
    build = subprocess.run(cabal_command(args, project_file, "build"), capture_output=True, text=True)
    if build.returncode != 0:
        sys.stderr.write(build.stdout)
        sys.stderr.write(build.stderr)
        return build.returncode

    located = subprocess.run(cabal_command(args, project_file, "list-bin"), capture_output=True, text=True)
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
