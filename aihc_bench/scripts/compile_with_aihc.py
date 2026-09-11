#!/usr/bin/env python3
"""Compile a benchmark's self-contained Cabal package with AIHC.

Invoked as the ``compile`` command for AIHC configurations in
``benchmark.json``. AIHC's ``build-exe``/``compile`` subcommands do not yet
parse a package directory's ``.cabal`` file the way ``install`` does (see
docs/aihc-cli-dependency-resolution.md for the requested follow-up), so this
script bridges the gap: it reads the benchmark's direct dependencies from its
``.cabal`` file and resolves their exact versions from
``cabal.project.freeze`` (the same file the GHC-side build uses, so both
toolchains compile identical versions).

``build-exe``'s ``-p`` flag only references a package already *installed* in
the AIHC store (it does not fetch or build anything itself — confirmed via
``aihc build-exe --help``), so this script runs ``aihc install`` for each
dependency before ``build-exe``. Boot-equivalent dependencies are already
installed once per commit by the runner's ``_prepare_aihc_store``, so
installing them again here is a cheap, idempotent no-op; non-boot
dependencies are genuinely built here, inside the timed compile step. This
whole approach is a temporary shim that should be deleted once AIHC's CLI
can resolve a package directory on its own.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from aihc_bench.freeze import AIHC_IMPLICIT_PACKAGES, parse_build_depends, parse_freeze  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, type=Path, help="AIHC compiler checkout")
    parser.add_argument("--source", required=True, type=Path, help="benchmark package directory")
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--gc", required=True)
    parser.add_argument("--build-command", required=True, choices=["build-exe", "compile"])
    parser.add_argument("--optimization", choices=["O0", "O1", "Os"], help="passed through as -O0/-O1/-Os; O2 is the default pipeline and needs no flag")
    parser.add_argument("--store", help="appended by the runner after this script's own arguments")
    parser.add_argument("--build-root", help="appended by the runner after this script's own arguments")
    args = parser.parse_args(argv)
    args.source = args.source.resolve()
    args.artifact = args.artifact.resolve()
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    cabal_files = list(args.source.glob("*.cabal"))
    if not cabal_files:
        sys.stderr.write(f"no .cabal file found in {args.source}\n")
        return 1
    freeze_file = args.source / "cabal.project.freeze"
    pinned = parse_freeze(freeze_file) if freeze_file.is_file() else {}
    dependencies = [
        (name, pinned[name])
        for name in parse_build_depends(cabal_files[0])
        if name not in AIHC_IMPLICIT_PACKAGES
    ]

    environment = dict(os.environ)
    nix_prefix = ["nix", "run", f"{args.worktree}#aihc", "--"]

    # build-exe's -p only references a package already installed in the
    # store (it does not resolve or build anything itself) — see
    # docs/aihc-cli-dependency-resolution.md. Boot-equivalent dependencies
    # are pre-installed once per commit by the runner's _prepare_aihc_store,
    # so installing them again here is a cheap, idempotent no-op; non-boot
    # dependencies (like snappy-hs) are genuinely built here, inside the
    # timed compile step, matching how cabal builds them live for GHC.
    for name, version in dependencies:
        install_command = nix_prefix + ["install", f"{name}-{version}", "--target", args.target]
        if args.store:
            install_command += ["--store", args.store]
        install_result = subprocess.run(install_command, cwd=args.worktree, env=environment, capture_output=True, text=True)
        if install_result.returncode != 0:
            sys.stderr.write(install_result.stdout)
            sys.stderr.write(install_result.stderr)
            return install_result.returncode

    command = nix_prefix + [args.build_command, str(args.source / "Main.hs")]
    command += ["--output", str(args.artifact), "--target", args.target, "--gc", args.gc]
    if args.optimization:
        command.append(f"-{args.optimization}")
    for name, version in dependencies:
        command += ["-p", f"{name}=={version}"]
    if args.store:
        command += ["--store", args.store]
    if args.build_root:
        command += ["--build-root", args.build_root]

    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, cwd=args.worktree, env=environment, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
