"""Parsing helpers for a benchmark's ``cabal.project.freeze`` and ``.cabal``
files.

Benchmarks are self-contained Cabal packages (see ``benchmarks/*``). The
GHC-side ``cabal build`` pins its Hackage dependencies from the freeze file;
``aihc build`` reads the package directory itself, and only needs the direct
``build-depends`` names so the runner can pre-install the ones GHC ships as
boot libraries.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

_CONSTRAINT = re.compile(r"any\.([A-Za-z0-9][A-Za-z0-9-]*)\s*==\s*([0-9][0-9A-Za-z.]*)")
_BUILD_DEPENDS_FIELD = re.compile(r"(?im)^\s*build-depends\s*:\s*(.*)$")
_CONTINUATION_LINE = re.compile(r"^\s+\S")


def parse_freeze(path: Path) -> Dict[str, str]:
    """Return ``{package_name: version}`` for every pinned constraint."""
    text = path.read_text(encoding="utf-8")
    return {name: version for name, version in _CONSTRAINT.findall(text)}


def parse_build_depends(cabal_path: Path) -> List[str]:
    """Return the direct dependency package names from a ``.cabal`` file.

    Assumes a single component (this repo's benchmarks each have exactly one
    executable stanza) and a ``build-depends`` field that is either a single
    line or continues on indented following lines, which covers every
    benchmark package in this repo without needing a full Cabal parser.
    """
    lines = cabal_path.read_text(encoding="utf-8").splitlines()
    values: List[str] = []
    collecting = False
    for line in lines:
        match = _BUILD_DEPENDS_FIELD.match(line)
        if match:
            values.append(match.group(1))
            collecting = True
            continue
        if collecting and _CONTINUATION_LINE.match(line):
            values.append(line)
            continue
        collecting = False
    joined = " ".join(values)
    names: List[str] = []
    for entry in joined.split(","):
        token = entry.strip().split()[0] if entry.strip() else ""
        if token and token not in names:
            names.append(token)
    return names


# Packages AIHC's ``build`` already treats as built in, mirroring what GHC
# ships for free. Kept in sync with core-libs/ in the AIHC compiler repo
# (base, ghc-internal, ghc-prim, system-cxx-std-lib, template-haskell).
AIHC_IMPLICIT_PACKAGES = frozenset(
    {"base", "ghc-internal", "ghc-prim", "system-cxx-std-lib", "template-haskell"}
)
