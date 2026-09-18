#!/usr/bin/env python3
"""Pin a Stackage snapshot as a JSON file the corpus derivation can fetch from.

The file lists every Hackage package in the snapshot with the SHA-256 of its
tarball, so that ``corpus/cpp/corpus.nix`` can fetch each one as a
fixed-output derivation without import-from-derivation or a network round
trip at evaluation time. It also records the compiler the snapshot was built
with and the versions of the packages that compiler ships (Stackage calls
them ``core``), which the generated ``cabal_macros.h`` files report for boot
libraries. Nothing is compiled: the snapshot is only read.

Inputs:

- the snapshot's package list from stackage.org, which names the compiler
  and carries every package's version, core packages included;
- the ``all-cabal-hashes`` tarball from nixpkgs, which carries the tarball
  hash of every Hackage release (``nix build nixpkgs#all-cabal-hashes``);
  a release newer than that tarball is looked up in the all-cabal-hashes
  repository on GitHub instead.

Usage:

    nix build --print-out-paths github:NixOS/nixpkgs/<rev>#all-cabal-hashes
    python3 corpus/stackage/update.py lts-24.58 \\
      --all-cabal-hashes /nix/store/...-all-cabal-hashes-...

The output goes to ``corpus/stackage/<snapshot>.json`` next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


def fetch_snapshot(snapshot: str) -> Dict[str, Any]:
    request = urllib.request.Request(f"https://www.stackage.org/{snapshot}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def upstream_hash(name: str, version: str) -> str:
    """The tarball hash from the all-cabal-hashes repository itself."""
    url = f"https://raw.githubusercontent.com/commercialhaskell/all-cabal-hashes/hackage/{name}/{version}/{name}.json"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))["package-hashes"]["SHA256"]


def tarball_hashes(archive: Path, wanted: Dict[str, str]) -> Dict[str, str]:
    """SHA-256 of ``<name>-<version>.tar.gz`` for every wanted package.

    The archive is streamed once; only the ``<name>/<version>/<name>.json``
    members of wanted packages are read.
    """
    remaining = {f"{name}/{version}/{name}.json": name for name, version in wanted.items()}
    hashes: Dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            relative = member.name.split("/", 1)[1] if "/" in member.name else member.name
            name = remaining.get(relative)
            if name is None:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            record = json.loads(handle.read().decode("utf-8"))
            hashes[name] = record["package-hashes"]["SHA256"]
            del remaining[relative]
            if not remaining:
                break
    return hashes


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("snapshot", help="Stackage snapshot name, for example lts-24.58")
    parser.add_argument("--all-cabal-hashes", required=True, type=Path, help="the nixpkgs all-cabal-hashes tarball")
    parser.add_argument("--snapshot-json", type=Path, help="a local copy of the snapshot's package list, instead of stackage.org")
    parser.add_argument("--output", type=Path, help="where to write the JSON (default: next to this script)")
    arguments = parser.parse_args(argv)

    if arguments.snapshot_json:
        listing = json.loads(arguments.snapshot_json.read_text(encoding="utf-8"))
    else:
        listing = fetch_snapshot(arguments.snapshot)
    compiler = listing["snapshot"]["compiler"]
    pinned = {entry["name"]: entry["version"] for entry in listing["packages"] if entry["origin"] == "hackage"}
    core = {entry["name"]: entry["version"] for entry in listing["packages"] if entry["origin"] != "hackage"}

    hashes = tarball_hashes(arguments.all_cabal_hashes, pinned)
    newer = sorted(set(pinned) - set(hashes))
    if newer:
        print(f"{len(newer)} releases are newer than the nixpkgs hash table; asking GitHub", file=sys.stderr)
    missing = []
    for name in newer:
        try:
            hashes[name] = upstream_hash(name, pinned[name])
        except (OSError, KeyError, ValueError) as error:
            print(f"warning: {name}-{pinned[name]}: {error}", file=sys.stderr)
            missing.append(name)
    if missing:
        print(f"warning: no tarball hash for {len(missing)} packages, left out: {', '.join(missing)}", file=sys.stderr)

    document = {
        "snapshot": arguments.snapshot,
        "compiler": compiler,
        "boot": dict(sorted(core.items())),
        "packages": [
            {"name": name, "version": pinned[name], "sha256": hashes[name]}
            for name in sorted(pinned)
            if name in hashes
        ],
    }
    output = arguments.output or Path(__file__).resolve().parent / f"{arguments.snapshot}.json"
    output.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"{output}: {len(document['packages'])} packages, {len(core)} boot libraries, {compiler}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
