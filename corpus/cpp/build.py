#!/usr/bin/env python3
"""Assemble the CPP corpus from a Stackage snapshot's package tarballs.

Runs inside the Nix derivation in ``corpus.nix``. Everything the benchmark
program needs to preprocess a module the way a real build would is decided
here, ahead of time, and written out as plain files, so the corpus can be read
by eye and the program that measures it stays small:

- ``<name>-<version>/``: the package's modules that use CPP, its ``.cabal``
  file, and every file those modules (and the files they include) ``#include``
  that the package itself ships;
- ``generated/<name>-<version>/cabal_macros.h``: what Cabal would generate for
  the package, with ``VERSION_`` and ``MIN_VERSION_`` for its dependencies at
  the snapshot's versions;
- ``generated/ghcversion.h``: what GHC pre-includes, with
  ``MIN_VERSION_GLASGOW_HASKELL`` for the snapshot's compiler;
- ``include/``: stand-ins for the headers GHC ships (``MachDeps.h`` and the
  like), searched last the way a compiler's own include directory is;
- ``macros.tsv``: the object-like macros GHC defines on its command line,
  ``__GLASGOW_HASKELL__`` and the platform family;
- ``modules.tsv``: one line per module with the files to pre-include and the
  directories to search for includes, all relative to the corpus root;
- ``benchmark.tsv``: the subset of ``modules.tsv`` the timed benchmark
  sweeps, chosen at an even stride through the corpus up to a byte budget, so
  one run fits the suite's per-process budget while spanning the snapshot;
  a budget of zero selects everything;
- ``packages.tsv``, ``report.txt`` and ``unresolved-includes.txt``: what went
  into the corpus and what could not be resolved, for inspection.

A module is selected when it contains a preprocessor directive; a module that
merely enables CPP without using it would only measure copying bytes. Includes
are resolved here as a report only; the benchmark resolves them again at run
time, because that is part of preprocessing.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Set, Tuple

_DIRECTIVES = ("if", "ifdef", "ifndef", "elif", "else", "endif", "define", "undef", "include")
_DIRECTIVE_LINE = re.compile(rb"^[ \t]*#[ \t]*(" + b"|".join(name.encode() for name in _DIRECTIVES) + rb")\b")
_INCLUDE_LINE = re.compile(rb'^[ \t]*#[ \t]*include[ \t]+(?:"([^"\n]+)"|<([^>\n]+)>)', re.MULTILINE)
_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*:(.*)$")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*")
_SANITIZE = re.compile(r"[^A-Za-z0-9_]")

# The platform GHC would define, fixed rather than taken from the host so that
# every machine preprocesses the same branches. A module that dispatches on
# platform otherwise falls through to its #error branch.
PLATFORM_MACROS = [
    ("x86_64_HOST_ARCH", "1"),
    ("x86_64_BUILD_ARCH", "1"),
    ("linux_HOST_OS", "1"),
    ("linux_BUILD_OS", "1"),
]


def uses_cpp(source: bytes) -> bool:
    return any(_DIRECTIVE_LINE.match(line) for line in source.split(b"\n"))


def included_paths(source: bytes) -> List[str]:
    """Every ``#include`` target in the file, conditional or not."""
    targets = []
    for match in _INCLUDE_LINE.finditer(source):
        target = match.group(1) or match.group(2)
        try:
            targets.append(target.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return targets


def cabal_fields(text: str, wanted: str) -> List[str]:
    """The values of every ``wanted`` field in a ``.cabal`` file, joined.

    A loose parser on purpose: it takes the field from every stanza and
    conditional branch, because the corpus wants a superset. A directory some
    other component would have used costs nothing; a missing one costs a
    module. A value may continue on lines indented deeper than the field.
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


def declared_include_dirs(cabal_text: str) -> List[str]:
    directories: List[str] = []
    for value in cabal_fields(cabal_text, "include-dirs"):
        for part in value.replace(",", " ").split():
            normalized = str(PurePosixPath(part))
            if normalized not in directories and not normalized.startswith("..") and not normalized.startswith("/"):
                directories.append(normalized)
    return directories


def declared_dependencies(cabal_text: str) -> List[str]:
    names: List[str] = []
    for value in cabal_fields(cabal_text, "build-depends"):
        for entry in value.split(","):
            match = _PACKAGE_NAME.match(entry.strip())
            if match:
                name = match.group(0)
                # A dependency on a sublibrary is written pkg:lib; keep pkg.
                if name not in names:
                    names.append(name)
    return names


def declared_cpp_defines(cabal_text: str) -> List[Tuple[str, str]]:
    """The ``-D`` flags of every ``cpp-options`` field, as name and value."""
    defines: List[Tuple[str, str]] = []
    for value in cabal_fields(cabal_text, "cpp-options"):
        for part in value.split():
            if not part.startswith("-D") or len(part) == 2:
                continue
            name, _, macro_value = part[2:].partition("=")
            if _SANITIZE.search(name):
                continue
            entry = (name, macro_value or "1")
            if entry not in defines:
                defines.append(entry)
    return defines


def version_tuple(version: str) -> List[int]:
    return [int(part) for part in version.split(".") if part.isdigit()]


def min_version_macro(name: str, version: str) -> str:
    """The ``MIN_VERSION_<pkg>`` and ``VERSION_<pkg>`` block Cabal writes."""
    macro = _SANITIZE.sub("_", name)
    parts = (version_tuple(version) + [0, 0, 0])[:3]
    major1, major2, minor = parts
    return (
        f"/* package {name}-{version} */\n"
        f"#ifndef VERSION_{macro}\n"
        f'#define VERSION_{macro} "{version}"\n'
        f"#endif /* VERSION_{macro} */\n"
        f"#ifndef MIN_VERSION_{macro}\n"
        f"#define MIN_VERSION_{macro}(major1,major2,minor) (\\\n"
        f"  (major1) <  {major1} || \\\n"
        f"  (major1) == {major1} && (major2) <  {major2} || \\\n"
        f"  (major1) == {major1} && (major2) == {major2} && (minor) <= {minor})\n"
        f"#endif /* MIN_VERSION_{macro} */\n"
    )


def tool_version_macro(tool: str, version: str) -> str:
    """The ``TOOL_VERSION_<tool>`` block Cabal writes for a configured program."""
    macro = _SANITIZE.sub("_", tool)
    major1, major2, minor = (version_tuple(version) + [0, 0, 0])[:3]
    return (
        f"/* tool {tool}-{version} */\n"
        f"#ifndef TOOL_VERSION_{macro}\n"
        f'#define TOOL_VERSION_{macro} "{version}"\n'
        f"#endif /* TOOL_VERSION_{macro} */\n"
        f"#ifndef MIN_TOOL_VERSION_{macro}\n"
        f"#define MIN_TOOL_VERSION_{macro}(major1,major2,minor) (\\\n"
        f"  (major1) <  {major1} || \\\n"
        f"  (major1) == {major1} && (major2) <  {major2} || \\\n"
        f"  (major1) == {major1} && (major2) == {major2} && (minor) <= {minor})\n"
        f"#endif /* MIN_TOOL_VERSION_{macro} */\n"
    )


def cabal_macros_header(
    package: str,
    version: str,
    dependencies: Iterable[str],
    versions: Dict[str, str],
    compiler: str,
    defines: Iterable[Tuple[str, str]],
) -> str:
    """A ``cabal_macros.h`` for one package, in Cabal's format.

    Only dependencies whose version the snapshot knows get a block; a name the
    snapshot does not carry (a typo, a Windows-only package) is listed in a
    comment so the omission can be seen. The compiler appears as a configured
    tool, as it does in Cabal's file, and the package's own ``cpp-options``
    defines come last: Cabal passes them on the command line, which is the
    same as defining them before the module starts.
    """
    lines = [
        "/* DO NOT EDIT: This file is automatically generated by the corpus build",
        f" * from the snapshot's versions of {package}'s build-depends. */",
        "",
    ]
    unknown = []
    for name in dependencies:
        if name in versions:
            lines.append(min_version_macro(name, versions[name]))
        else:
            unknown.append(name)
    if unknown:
        lines.append("/* not in the snapshot: " + " ".join(unknown) + " */")
        lines.append("")
    compiler_version = compiler.removeprefix("ghc-")
    for tool in ("ghc", "ghc-pkg", "hsc2hs"):
        lines.append(tool_version_macro(tool, compiler_version))
    lines.append(f"/* package {package}-{version} itself */")
    lines.append("#ifndef CURRENT_PACKAGE_KEY")
    lines.append(f'#define CURRENT_PACKAGE_KEY "{package}-{version}"')
    lines.append("#endif /* CURRENT_PACKAGE_KEY */")
    lines.append("#ifndef CURRENT_PACKAGE_VERSION")
    lines.append(f'#define CURRENT_PACKAGE_VERSION "{version}"')
    lines.append("#endif /* CURRENT_PACKAGE_VERSION */")
    lines.append("")
    defines = list(defines)
    if defines:
        lines.append(f"/* cpp-options of {package} */")
        for name, macro_value in defines:
            lines.append(f"#define {name} {macro_value}")
        lines.append("")
    return "\n".join(lines)


def ghc_version_macros(compiler: str) -> Tuple[List[Tuple[str, str]], str]:
    """The ``__GLASGOW_HASKELL__`` family and a ``ghcversion.h`` for it.

    GHC passes the object-like macros with ``-D`` and pre-includes the header
    for the function-like ``MIN_VERSION_GLASGOW_HASKELL``.
    """
    version = compiler.removeprefix("ghc-")
    parts = (version_tuple(version) + [0, 0, 0, 0])[:4]
    major, minor, patch1, patch2 = parts
    encoded = str(major * 100 + minor)
    macros = [
        ("__GLASGOW_HASKELL__", encoded),
        ("__GLASGOW_HASKELL_FULL_VERSION__", f'"{version}"'),
        ("__GLASGOW_HASKELL_PATCHLEVEL1__", str(patch1)),
        ("__GLASGOW_HASKELL_PATCHLEVEL2__", str(patch2)),
    ]
    header = (
        "/* Generated by the corpus build: what GHC's ghcversion.h defines for\n"
        f" * {compiler}. GHC pre-includes this header into every module. */\n"
        "#ifndef __GHCVERSION_H__\n"
        "#define __GHCVERSION_H__\n"
        "\n"
        "#ifndef __GLASGOW_HASKELL__\n"
        f"# define __GLASGOW_HASKELL__ {encoded}\n"
        "#endif\n"
        "\n"
        f"#define __GLASGOW_HASKELL_PATCHLEVEL1__ {patch1}\n"
        f"#define __GLASGOW_HASKELL_PATCHLEVEL2__ {patch2}\n"
        "\n"
        "#define MIN_VERSION_GLASGOW_HASKELL(ma,mi,pl1,pl2) (\\\n"
        f"   ((ma)*100+(mi)) <  {encoded} || \\\n"
        f"   ((ma)*100+(mi)) == {encoded} && (pl1) <  {patch1} || \\\n"
        f"   ((ma)*100+(mi)) == {encoded} && (pl1) == {patch1} && (pl2) <= {patch2} )\n"
        "\n"
        "#endif /* __GHCVERSION_H__ */\n"
    )
    return macros, header


class Package:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self.directory = f"{name}-{version}"
        self.files: Dict[str, bytes] = {}
        self.cabal_file: Optional[str] = None
        self.modules: List[str] = []
        self.include_dirs: List[str] = []
        self.dependencies: List[str] = []
        self.defines: List[Tuple[str, str]] = []
        self.kept: Set[str] = set()
        self.unresolved: List[Tuple[str, str]] = []


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
    cabal_files = sorted(name for name in package.files if "/" not in name and name.endswith(".cabal"))
    cabal_text = ""
    if cabal_files:
        package.cabal_file = cabal_files[0]
        package.kept.add(package.cabal_file)
        cabal_text = package.files[package.cabal_file].decode("utf-8", errors="replace")
    package.include_dirs = declared_include_dirs(cabal_text)
    package.dependencies = declared_dependencies(cabal_text)
    package.defines = declared_cpp_defines(cabal_text)
    for name in sorted(package.files):
        if name.endswith(".hs") and "\t" not in name and ":" not in name and uses_cpp(package.files[name]):
            package.modules.append(name)
            package.kept.add(name)


def search_dirs(package: Package, from_file: str) -> List[str]:
    """Where ``#include`` looks from a file, in order, relative to the package.

    The including file's own directory first, as any C preprocessor does;
    then what the package declares in ``include-dirs``; then ``include`` and
    the package root, which a bare tree often relies on. ``modules.tsv`` adds
    the package's generated directory, so an explicit ``#include
    "cabal_macros.h"`` resolves as it does under Cabal, and the corpus-wide
    stand-in directory after all of these.
    """
    own = str(PurePosixPath(from_file).parent)
    candidates = [own] + package.include_dirs + ["include", "."]
    ordered: List[str] = []
    for candidate in candidates:
        normalized = str(PurePosixPath(candidate)) if candidate not in ("", ".") else "."
        if normalized not in ordered:
            ordered.append(normalized)
    return ordered


def resolve(package: Package, from_file: str, target: str) -> Optional[str]:
    if target.startswith("/"):
        return None
    for directory in search_dirs(package, from_file):
        candidate = str(PurePosixPath(directory) / target) if directory != "." else str(PurePosixPath(target))
        if candidate in package.files and ".." not in PurePosixPath(candidate).parts:
            return candidate
    return None


def collect_includes(package: Package, stubs: Set[str]) -> None:
    """Keep every file reachable through ``#include`` from a kept module."""
    pending = list(package.modules)
    seen: Set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for target in included_paths(package.files[current]):
            found = resolve(package, current, target)
            if found is not None:
                package.kept.add(found)
                pending.append(found)
            elif PurePosixPath(target).name not in stubs and target not in stubs:
                package.unresolved.append((current, target))


def write_package(package: Package, out: Path, versions: Dict[str, str], compiler: str) -> None:
    root = out / package.directory
    for name in sorted(package.kept):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(package.files[name])
    generated = out / "generated" / package.directory
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "cabal_macros.h").write_text(
        cabal_macros_header(package.name, package.version, package.dependencies, versions, compiler, package.defines),
        encoding="utf-8",
    )


def sample(lines: List[Tuple[int, str]], budget: int) -> List[str]:
    """Modules for the timed sweep: an even stride through the corpus, up to
    ``budget`` bytes of module source. Zero means every module.

    A stride rather than a prefix, so the sample spans every package instead
    of stopping inside whichever sorts first; deterministic, so every machine
    measures the same modules.
    """
    total_bytes = sum(size for size, _ in lines)
    if budget <= 0 or total_bytes <= budget:
        return [line for _, line in lines]
    step = max(1, total_bytes // budget)
    chosen: List[str] = []
    taken = 0
    for index in range(0, len(lines), step):
        size, line = lines[index]
        if taken + size > budget:
            break
        chosen.append(line)
        taken += size
    return chosen


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True, type=Path, help="the pinned snapshot JSON")
    parser.add_argument("--tarballs", required=True, type=Path, help="JSON mapping <name>-<version> to a tarball path")
    parser.add_argument("--include", required=True, type=Path, help="directory of stand-in headers")
    parser.add_argument("--out", required=True, type=Path, help="corpus root to write")
    parser.add_argument("--sample-bytes", type=int, default=0, help="byte budget of benchmark.tsv; 0 selects every module")
    arguments = parser.parse_args(argv)

    snapshot = json.loads(arguments.snapshot.read_text(encoding="utf-8"))
    tarballs: Dict[str, str] = json.loads(arguments.tarballs.read_text(encoding="utf-8"))
    versions: Dict[str, str] = dict(snapshot.get("boot", {}))
    versions.update({entry["name"]: entry["version"] for entry in snapshot["packages"]})
    stubs = {path.name for path in arguments.include.iterdir() if path.is_file()} | {"ghcversion.h", "cabal_macros.h"}

    out = arguments.out
    out.mkdir(parents=True)
    shutil.copytree(arguments.include, out / "include")
    macros, ghcversion = ghc_version_macros(snapshot["compiler"])
    (out / "generated").mkdir()
    (out / "generated" / "ghcversion.h").write_text(ghcversion, encoding="utf-8")
    with (out / "macros.tsv").open("w", encoding="utf-8") as handle:
        for name, value in macros + PLATFORM_MACROS:
            handle.write(f"{name}\t{value}\n")

    errors: List[str] = []
    packages: List[Package] = []
    module_count = 0
    module_bytes = 0
    unresolved_names: Counter = Counter()
    module_lines: List[Tuple[int, str]] = []
    packages_out = io.StringIO()
    packages_out.write("package\tversion\tmodules\tinclude-dirs\tunresolved-includes\n")
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
        if not package.modules:
            continue
        collect_includes(package, stubs)
        write_package(package, out, versions, snapshot["compiler"])
        packages.append(package)
        for module in package.modules:
            module_count += 1
            module_bytes += len(package.files[module])
            preludes = ["generated/ghcversion.h", f"generated/{package.directory}/cabal_macros.h"]
            dirs = [f"{package.directory}/{d}" if d != "." else package.directory for d in search_dirs(package, module)[1:]]
            dirs.extend([f"generated/{package.directory}", "include"])
            line = f"{package.directory}\t{package.directory}/{module}\t{':'.join(preludes)}\t{':'.join(dirs)}\n"
            module_lines.append((len(package.files[module]), line))
        for from_file, target in package.unresolved:
            unresolved_names[target] += 1
        packages_out.write(
            f"{package.name}\t{package.version}\t{len(package.modules)}\t{' '.join(package.include_dirs)}\t{len(package.unresolved)}\n"
        )
        package.files.clear()

    (out / "modules.tsv").write_text("".join(line for _, line in module_lines), encoding="utf-8")
    sampled = sample(module_lines, arguments.sample_bytes)
    (out / "benchmark.tsv").write_text("".join(sampled), encoding="utf-8")
    (out / "packages.tsv").write_text(packages_out.getvalue(), encoding="utf-8")
    with (out / "unresolved-includes.txt").open("w", encoding="utf-8") as handle:
        handle.write("# #include targets no kept file of the package and no stand-in header satisfies.\n")
        handle.write("# Many sit under a platform conditional the corpus never takes.\n")
        for package in packages:
            for from_file, target in package.unresolved:
                handle.write(f"{package.directory}/{from_file}\t{target}\n")
    with (out / "report.txt").open("w", encoding="utf-8") as handle:
        handle.write(f"snapshot: {snapshot['snapshot']} ({snapshot['compiler']})\n")
        handle.write(f"packages in snapshot: {len(snapshot['packages'])}\n")
        handle.write(f"packages with CPP modules: {len(packages)}\n")
        handle.write(f"modules: {module_count}\n")
        handle.write(f"module bytes: {module_bytes}\n")
        handle.write(f"benchmark.tsv: {len(sampled)} modules within {arguments.sample_bytes or 'no'} byte budget\n")
        handle.write(f"unresolved includes: {sum(unresolved_names.values())}\n")
        handle.write(f"tarball errors: {len(errors)}\n")
        for error in errors:
            handle.write(f"  {error}\n")
        handle.write("\nmost common unresolved includes:\n")
        for target, count in unresolved_names.most_common(40):
            handle.write(f"  {count:5} {target}\n")
    print((out / "report.txt").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
