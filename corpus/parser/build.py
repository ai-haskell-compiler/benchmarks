#!/usr/bin/env python3
"""Assemble the parser corpus from a Stackage snapshot's package tarballs.

Runs inside the Nix derivation in ``corpus.nix``. The corpus is every Haskell
module of the snapshot that a parser can read as it sits on disk: one that
each package's ``.cabal`` file declares (so test fixtures that are meant not
to parse stay out), that is plain ``.hs`` rather than ``.lhs`` or ``.hsc``,
that is valid UTF-8 without a byte-order mark, and that no preprocessor
touches -- neither CPP, enabled by a ``LANGUAGE`` pragma or by the package's
``default-extensions``, nor a custom one named by ``-pgmF``. Everything the
benchmark program needs to parse a module the way a build would is decided
here and written out as plain files:

- ``<name>-<version>/``: the package's selected modules, at their paths in
  the release tarball;
- ``modules.tsv``: one line per module with the package, the module's path,
  the ``default-language`` of the package and its ``default-extensions``,
  which are the settings a build would give the parser before the module's
  own ``LANGUAGE`` pragmas;
- ``benchmark.tsv``: the subset of ``modules.tsv`` the timed benchmark
  sweeps, chosen at an even stride through the corpus up to a byte budget, so
  one run fits the suite's per-process budget while spanning the snapshot;
  a budget of zero selects everything;
- ``packages.tsv`` and ``report.txt``: what went into the corpus and what was
  left out, for inspection.

The cabal file is read loosely, as the CPP corpus reads it: every stanza and
conditional branch contributes, because the corpus wants a superset. A
``default-extensions`` entry that only one component uses costs nothing
worse than parsing the others with it on, while a missed one costs a parse
error. The one exception is ``CPP``: a package that enables it anywhere is
left out entirely, since a module of that package may rely on the
preprocessor without saying so itself.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*:(.*)$")
_MODULE_NAME = re.compile(r"^[A-Z][A-Za-z0-9_']*(\.[A-Z][A-Za-z0-9_']*)*$")
_EXTENSION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_LANGUAGE_PRAGMA = re.compile(rb"\{-#\s*LANGUAGE\b(.*?)#-\}", re.IGNORECASE | re.DOTALL)
_OPTIONS_PRAGMA = re.compile(rb"\{-#\s*OPTIONS(?:_GHC)?\b(.*?)#-\}", re.IGNORECASE | re.DOTALL)
_CPP_SETTING = re.compile(rb"(?<![A-Za-z0-9_])CPP(?![A-Za-z0-9_])")
_PREPROCESSOR_OPTION = re.compile(rb"(?<!\S)-(?:F|pgmF|optF)(?!\S)|(?<!\S)-pgmF\S|(?<!\S)-XCPP(?!\S)")
_DIRECTIVES = ("if", "ifdef", "ifndef", "elif", "else", "endif", "define", "undef", "include")
_DIRECTIVE_LINE = re.compile(rb"^[ \t]*#[ \t]*(" + b"|".join(name.encode() for name in _DIRECTIVES) + rb")\b", re.MULTILINE)


def cabal_fields(text: str, wanted: str) -> List[str]:
    """The values of every ``wanted`` field in a ``.cabal`` file, joined.

    A loose parser on purpose: it takes the field from every stanza and
    conditional branch, because the corpus wants a superset. A value may
    continue on lines indented deeper than the field.
    """
    values: List[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _FIELD.match(line.strip())
        if match and match.group(1).lower() == wanted:
            indent = len(line) - len(line.lstrip())
            values.append(match.group(2))
            index += 1
            while index < len(lines):
                following = lines[index]
                stripped = following.strip()
                if stripped and (len(following) - len(following.lstrip())) > indent and not _FIELD.match(stripped):
                    values.append(stripped)
                    index += 1
                elif not stripped:
                    index += 1
                else:
                    break
            continue
        index += 1
    return values


def field_words(cabal_text: str, *names: str) -> List[str]:
    """Every comma- or space-separated word of the named fields, in order."""
    words: List[str] = []
    for name in names:
        for value in cabal_fields(cabal_text, name):
            for word in value.replace(",", " ").split():
                if word not in words:
                    words.append(word)
    return words


def declared_source_dirs(cabal_text: str) -> List[str]:
    directories: List[str] = []
    for word in field_words(cabal_text, "hs-source-dirs"):
        normalized = str(PurePosixPath(word))
        if normalized.startswith("..") or normalized.startswith("/"):
            continue
        if normalized not in directories:
            directories.append(normalized)
    return directories or ["."]


def declared_modules(cabal_text: str) -> List[str]:
    """Module names from ``exposed-modules`` and ``other-modules``, as paths."""
    paths: List[str] = []
    for word in field_words(cabal_text, "exposed-modules", "other-modules"):
        if _MODULE_NAME.match(word):
            path = word.replace(".", "/") + ".hs"
            if path not in paths:
                paths.append(path)
    for word in field_words(cabal_text, "main-is"):
        normalized = str(PurePosixPath(word))
        if normalized.endswith(".hs") and not normalized.startswith("..") and not normalized.startswith("/") and normalized not in paths:
            paths.append(normalized)
    return paths


def declared_extensions(cabal_text: str) -> List[str]:
    """``default-extensions`` and the older ``extensions`` field, as written."""
    return [word for word in field_words(cabal_text, "default-extensions", "extensions") if _EXTENSION_NAME.match(word)]


def declared_language(cabal_text: str) -> str:
    """The first ``default-language``; the library stanza usually comes first."""
    for word in field_words(cabal_text, "default-language"):
        if _EXTENSION_NAME.match(word):
            return word
    return ""


def preprocessed(source: bytes) -> Optional[str]:
    """Why a preprocessor would run on this module, or ``None`` if none would.

    ``CPP`` in any ``LANGUAGE`` pragma, or ``-XCPP``, ``-F`` or ``-pgmF`` in
    an ``OPTIONS`` pragma: GHC would rewrite the file before parsing it, so
    the bytes on disk are not what a parser is meant to read. A preprocessor
    directive without either says the package turns CPP on some way the
    cabal file's fields do not show (``ghc-options: -cpp``, say), and counts
    the same.
    """
    for pragma in _LANGUAGE_PRAGMA.finditer(source):
        if _CPP_SETTING.search(pragma.group(1)):
            return "cpp"
    if _DIRECTIVE_LINE.search(source):
        return "cpp"
    for pragma in _OPTIONS_PRAGMA.finditer(source):
        if _PREPROCESSOR_OPTION.search(pragma.group(1)):
            return "preprocessor"
    return None


def readable(source: bytes) -> bool:
    """Valid UTF-8 without a byte-order mark, which is what the program decodes."""
    if source.startswith(b"\xef\xbb\xbf"):
        return False
    try:
        source.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


class Package:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self.directory = f"{name}-{version}"
        self.files: Dict[str, bytes] = {}
        self.modules: List[str] = []
        self.language = ""
        self.extensions: List[str] = []
        self.skipped: Dict[str, int] = {}


def read_tarball(path: Path, package: Package) -> Optional[str]:
    """Load the regular files of a Hackage tarball; returns an error, if any."""
    prefix = package.directory + "/"
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if not member.isfile() or not member.name.startswith(prefix):
                    continue
                relative = member.name[len(prefix):]
                if not relative or ".." in PurePosixPath(relative).parts:
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    package.files[relative] = handle.read()
    except (tarfile.TarError, EOFError, OSError) as error:
        return f"{package.directory}: unreadable tarball: {error}"
    if not package.files:
        return f"{package.directory}: tarball holds no files under {prefix}"
    return None


def select(package: Package) -> None:
    """Choose the package's modules; ``package.skipped`` says what was not."""
    cabal_files = sorted(name for name in package.files if "/" not in name and name.endswith(".cabal"))
    if not cabal_files:
        package.skipped["no-cabal-file"] = 1
        return
    cabal_text = package.files[cabal_files[0]].decode("utf-8", errors="replace")
    package.language = declared_language(cabal_text)
    package.extensions = declared_extensions(cabal_text)
    if "CPP" in package.extensions:
        package.skipped["package-enables-cpp"] = 1
        return
    directories = declared_source_dirs(cabal_text)
    seen: Set[str] = set()
    for declared in declared_modules(cabal_text):
        for directory in directories:
            candidate = declared if directory == "." else str(PurePosixPath(directory) / declared)
            if candidate in package.files and candidate not in seen:
                seen.add(candidate)
                source = package.files[candidate]
                reason = "not-utf8" if not readable(source) else preprocessed(source)
                if reason is None:
                    package.modules.append(candidate)
                else:
                    package.skipped[reason] = package.skipped.get(reason, 0) + 1
                break
    package.modules.sort()


def write_package(package: Package, out: Path) -> None:
    root = out / package.directory
    for name in package.modules:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(package.files[name])


#: The largest module the timed sample takes. Generated modules run to
#: hundreds of kilobytes and one of them would be the whole sample; the sweep
#: is meant to span the snapshot's hand-written code, which mostly sits well
#: under this.
SAMPLE_MODULE_LIMIT = 16 * 1024


def sample(lines: List[Tuple[int, str]], budget: int) -> List[str]:
    """Modules for the timed sweep: an even stride through the corpus, up to
    ``budget`` bytes of module source. Zero means every module.

    A stride rather than a prefix, so the sample spans every package instead
    of stopping inside whichever sorts first; deterministic, so every machine
    measures the same modules. Modules over ``SAMPLE_MODULE_LIMIT`` are
    stepped over, so the sample is many modules rather than one large one.
    """
    total_bytes = sum(size for size, _ in lines)
    if budget <= 0 or total_bytes <= budget:
        return [line for _, line in lines]
    candidates = [(size, line) for size, line in lines if size <= SAMPLE_MODULE_LIMIT]
    step = max(1, sum(size for size, _ in candidates) // budget)
    chosen: List[str] = []
    taken = 0
    for index in range(0, len(candidates), step):
        size, line = candidates[index]
        if taken + size > budget:
            break
        chosen.append(line)
        taken += size
    return chosen


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True, type=Path, help="the pinned snapshot JSON")
    parser.add_argument("--tarballs", required=True, type=Path, help="JSON mapping <name>-<version> to a tarball path")
    parser.add_argument("--out", required=True, type=Path, help="corpus root to write")
    parser.add_argument("--sample-bytes", type=int, default=0, help="byte budget of benchmark.tsv; 0 selects every module")
    arguments = parser.parse_args(argv)

    snapshot = json.loads(arguments.snapshot.read_text(encoding="utf-8"))
    tarballs: Dict[str, str] = json.loads(arguments.tarballs.read_text(encoding="utf-8"))

    out = arguments.out
    out.mkdir(parents=True)

    errors: List[str] = []
    packages_with_modules = 0
    module_bytes = 0
    skipped: Dict[str, int] = {}
    module_lines: List[Tuple[int, str]] = []
    packages_out = io.StringIO()
    packages_out.write("package\tversion\tmodules\tlanguage\textensions\tskipped\n")
    for entry in snapshot["packages"]:
        package = Package(entry["name"], entry["version"])
        tarball = tarballs.get(package.directory)
        if tarball is None:
            errors.append(f"{package.directory}: no tarball was fetched")
            continue
        error = read_tarball(Path(tarball), package)
        if error:
            errors.append(error)
            continue
        select(package)
        for reason, count in package.skipped.items():
            skipped[reason] = skipped.get(reason, 0) + count
        if package.modules:
            packages_with_modules += 1
            write_package(package, out)
        for module in package.modules:
            size = len(package.files[module])
            module_bytes += size
            line = f"{package.directory}\t{package.directory}/{module}\t{package.language}\t{':'.join(package.extensions)}\n"
            module_lines.append((size, line))
        packages_out.write(
            f"{package.name}\t{package.version}\t{len(package.modules)}\t{package.language}\t{' '.join(package.extensions)}\t"
            f"{' '.join(f'{reason}={count}' for reason, count in sorted(package.skipped.items()))}\n"
        )
        package.files.clear()

    (out / "modules.tsv").write_text("".join(line for _, line in module_lines), encoding="utf-8")
    sampled = sample(module_lines, arguments.sample_bytes)
    (out / "benchmark.tsv").write_text("".join(sampled), encoding="utf-8")
    (out / "packages.tsv").write_text(packages_out.getvalue(), encoding="utf-8")
    with (out / "report.txt").open("w", encoding="utf-8") as handle:
        handle.write(f"snapshot: {snapshot['snapshot']} ({snapshot['compiler']})\n")
        handle.write(f"packages in snapshot: {len(snapshot['packages'])}\n")
        handle.write(f"packages with modules: {packages_with_modules}\n")
        handle.write(f"modules: {len(module_lines)}\n")
        handle.write(f"module bytes: {module_bytes}\n")
        handle.write(f"benchmark.tsv: {len(sampled)} modules within {arguments.sample_bytes or 'no'} byte budget\n")
        handle.write(f"tarball errors: {len(errors)}\n")
        for error in errors:
            handle.write(f"  {error}\n")
        handle.write("\nleft out:\n")
        for reason, count in sorted(skipped.items()):
            handle.write(f"  {count:6} {reason}\n")
    print((out / "report.txt").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
