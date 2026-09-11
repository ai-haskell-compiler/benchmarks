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
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


# The cabal -O level for each benchmark profile. GHC has no size-oriented
# level, so the Os profile records what GHC produces at -O1.
GHC_LEVELS = {"O0": "O0", "O1": "O1", "O2": "O2", "Os": "O1"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="benchmark package directory")
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--exe", required=True, help="cabal executable component name")
    parser.add_argument("--ghc", required=True, help="path to the pinned ghc binary")
    parser.add_argument("--cabal", default="cabal", help="cabal-install binary (default: cabal on PATH)")
    parser.add_argument("--optimization", required=True, choices=["O0", "O1", "O2", "Os"], help="benchmark profile; GHC has no size level, so Os builds with -O1")
    parser.add_argument("--ghc-option", action="append", default=[], help="extra -f.../-rtsopts flag, repeatable")
    args = parser.parse_args(argv)
    args.source = args.source.resolve()
    args.build_dir = args.build_dir.resolve()
    args.artifact = args.artifact.resolve()
    args.ghc_level = GHC_LEVELS[args.optimization]
    return args


def generate_project_file(args: argparse.Namespace) -> Path:
    args.build_dir.mkdir(parents=True, exist_ok=True)
    project_file = args.build_dir / "cabal.project"
    freeze_file = args.source / "cabal.project.freeze"
    contents = [f"packages: {args.source}\n"]
    if freeze_file.is_file():
        contents.append(f"\nimport: {freeze_file}\n")
    project_file.write_text("".join(contents), encoding="utf-8")
    return project_file


def cabal_command(args: argparse.Namespace, project_file: Path) -> list[str]:
    ghc_options = list(args.ghc_option)
    command = [
        args.cabal,
        "build",
        f"--project-file={project_file}",
        f"--builddir={args.build_dir / 'dist'}",
        f"--with-compiler={args.ghc}",
        f"-{args.ghc_level}",
    ]
    if ghc_options:
        command.append("--ghc-options=" + " ".join(ghc_options))
    command.append(f"exe:{args.exe}")
    return command


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    project_file = generate_project_file(args)
    build_command = cabal_command(args, project_file)
    build = subprocess.run(build_command, capture_output=True, text=True)
    if build.returncode != 0:
        sys.stderr.write(build.stdout)
        sys.stderr.write(build.stderr)
        return build.returncode

    list_bin_command = [
        args.cabal,
        "list-bin",
        f"--project-file={project_file}",
        f"--builddir={args.build_dir / 'dist'}",
        f"--with-compiler={args.ghc}",
        f"-{args.ghc_level}",
        f"exe:{args.exe}",
    ]
    located = subprocess.run(list_bin_command, capture_output=True, text=True)
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
