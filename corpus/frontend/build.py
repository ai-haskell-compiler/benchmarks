#!/usr/bin/env python3
"""Build a closed, offline frontend workload from complete Stackage libraries.

Only the unconditional, preprocessor-free libraries selected in corpus.nix
are supported here. Fail rather than guess at Cabal conditionals. Core sources
come from the same pinned AIHC revision as the frontend libraries. Their Linux
x86_64 variant is used on every host, so the input bytes remain comparable.
"""
import argparse
import importlib.util
import json
import re
import shutil
from pathlib import Path

WIRED = "GHC.CString GHC.Classes GHC.Prim GHC.Prim.Base GHC.Prim.Enum GHC.Prim.MonadFail GHC.Prim.MonadFix GHC.Prim.Num GHC.Prim.Real GHC.Prim.String GHC.Tuple GHC.Types".split()
IMPORT = re.compile(r'^import\s+(?:\{-#.*?#-\}\s*)?(?:qualified\s+)?(?:"[^"]+"\s+)?([A-Z][\w\'.]*)', re.M)


def assemble(packages, core, out, parser_script):
    spec = importlib.util.spec_from_file_location("parser_corpus", parser_script)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    out.mkdir(parents=True, exist_ok=True)
    sources = {}
    metadata = {}
    for directory in packages:
        cabal, = directory.glob("*.cabal")
        text = cabal.read_text()
        # Take exactly the public library, excluding tests and executables.
        match = re.search(r'^library\s*\n((?:[ \t].*\n|\n)*)', text, re.I | re.M)
        if not match:
            raise ValueError(f"{cabal}: no public library")
        library = match[1]
        if re.search(r'^\s+(?:if|elif|else|import)\b', library, re.M | re.I):
            raise ValueError(f"{cabal}: conditional/common library settings unsupported")
        name = parser.field_words(text, "name")[0]
        edition = parser.declared_language(library) or "Haskell98"
        extensions = parser.declared_extensions(library)
        dependencies = set(re.findall(r'(?:^|,)\s*([a-zA-Z][\w-]*)', ','.join(parser.cabal_fields(library, "build-depends"))))
        if dependencies != {"base"}:
            raise ValueError(f"{cabal}: expected base-only library, got {dependencies}")
        metadata[name] = (["aihc-base"], edition, extensions)
        for path in parser.declared_modules(library):
            candidates = [directory / source / path for source in parser.declared_source_dirs(library)]
            source = next((p for p in candidates if p.is_file()), None)
            if source is None:
                raise ValueError(f"{cabal}: missing module {path}")
            data = source.read_bytes()
            if not parser.readable(data) or parser.preprocessed(data) or "CPP" in extensions:
                raise ValueError(f"{source}: not a preprocessor-free UTF-8 module")
            sources[(name, path[:-3].replace('/', '.'))] = (source, edition, extensions)
        (out / name).mkdir(exist_ok=True)
        for document in [cabal, *directory.glob("LICENSE*")]:
            shutil.copyfile(document, out / name / document.name)
    roots = list(sources)
    providers = {}
    for name, dirs in [("aihc-prim", ["src"]), ("aihc-base", ["src-linux", "src-x86_64", "src-posix", "src"])]:
        metadata[name] = ([] if name == "aihc-prim" else ["aihc-prim"], "GHC2021", ["NoImplicitPrelude"])
        for source_dir in dirs:
            for source in sorted((core / name / source_dir).rglob("*.hs")):
                module = str(source.relative_to(core / name / source_dir))[:-3].replace('/', '.')
                providers.setdefault(module, (name, source))
    pending = [module for _, module in roots] + WIRED
    root_names = {module: name for name, module in roots}
    visited = set()
    imports = {}
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        if module in root_names:
            key = (root_names[module], module)
            source, edition, extensions = sources[key]
        elif module in providers:
            name, source = providers[module]
            key = (name, module)
            if key in sources:
                continue
            _, edition, extensions = metadata[name]
            sources[key] = (source, edition, extensions)
        else:
            raise ValueError(f"no provider for imported module {module}")
        text = source.read_text()
        found = IMPORT.findall(text)
        if "NoImplicitPrelude" not in extensions and "NoImplicitPrelude" not in text:
            found.append("Prelude")
        pending.extend(found)
        imports[key] = sorted(set(found + WIRED) - {module})
    lines = []
    for name, module in dependency_order(sources, imports, metadata):
        source, edition, extensions = sources[(name, module)]
        relative = name + "/" + module.replace('.', '/') + ".hs"
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        lines.append('\t'.join([name, relative, edition, ':'.join(extensions)]) + '\n')
    (out / "modules.tsv").write_text(''.join(lines))
    (out / "packages.tsv").write_text(''.join(name + '\t' + ':'.join(deps) + '\n' for name, (deps, _, _) in sorted(metadata.items())))
    (out / "README.txt").write_text("Complete colour and split libraries from Stackage lts-24.58, with their transitive AIHC core source closure. Fixed Linux x86_64 source variant on every benchmark target.\n")
    return len(sources)


def dependency_order(sources, imports, metadata):
    """Every module after the modules it may depend on, packages in dependency order.

    Dependencies are over-approximated like the benchmark's own module graph:
    imports, implicit Prelude and every wired type module. A cyclic group is
    kept contiguous, so a prefix of the order that ends on a group boundary
    is closed under dependencies; write_workloads checks the prefixes used.
    """
    package_order = []
    def visit_package(name, path=()):
        if name in package_order:
            return
        if name in path:
            raise ValueError(f"cyclic package dependency at {name}")
        for dependency in sorted(metadata[name][0]):
            visit_package(dependency, path + (name,))
        package_order.append(name)
    for name in sorted(metadata):
        visit_package(name)
    order = []
    for package in package_order:
        members = sorted(module for name, module in sources if name == package)
        local = set(members)
        edges = {module: [d for d in imports[(package, module)] if d in local] for module in members}
        index, low, stack, on_stack, counter = {}, {}, [], set(), [0]
        def strongconnect(module):
            # Iterative Tarjan: SCCs come out dependencies first.
            work = [(module, iter(edges[module]))]
            index[module] = low[module] = counter[0]
            counter[0] += 1
            stack.append(module)
            on_stack.add(module)
            while work:
                node, children = work[-1]
                child = next(children, None)
                if child is not None:
                    if child not in index:
                        index[child] = low[child] = counter[0]
                        counter[0] += 1
                        stack.append(child)
                        on_stack.add(child)
                        work.append((child, iter(edges[child])))
                    elif child in on_stack:
                        low[node] = min(low[node], index[child])
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    group = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.append(member)
                        if member == node:
                            break
                    order.extend((package, member) for member in sorted(group))
        for module in members:
            if module not in index:
                strongconnect(module)
    return order


def write_workloads(path, out):
    cells = json.loads(path.read_text())
    modules = [line.split('\t') for line in (out / 'modules.tsv').read_text().splitlines()]
    if set(cells) != {'resolve', 'tc'}:
        raise ValueError('expected resolve and tc workloads')
    lines = []
    for phase, backends in cells.items():
        if set(backends) != {'native', 'llvm', 'wasm'}:
            raise ValueError('expected all three backends')
        for backend, profiles in backends.items():
            if set(profiles) != {'O0', 'Os', 'O1', 'O2'}:
                raise ValueError('expected all four profiles')
            for profile, cell in profiles.items():
                count = cell['iterations']
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise ValueError('iterations must be a positive integer')
                prefix = cell['modules']
                if isinstance(prefix, bool) or not isinstance(prefix, int) or not 1 <= prefix <= len(modules):
                    raise ValueError('modules must be a prefix length within the corpus')
                if not prefix_is_closed(out, modules, prefix):
                    raise ValueError(f'{phase} {backend} {profile}: a module prefix of {prefix} splits a dependency')
                if not cell['expected'] or any(c in cell['expected'] for c in '\t\n\r'):
                    raise ValueError('expected a single-line reference tally')
                lines.append('\t'.join([phase, backend, profile, str(count), str(prefix), cell['expected']]) + '\n')
    (out / 'workloads.tsv').write_text(''.join(lines))


def prefix_is_closed(out, modules, prefix):
    """Whether the first PREFIX modules.tsv entries contain every corpus module they may import."""
    names = {entry[1]: entry[1].split('/', 1)[1][:-3].replace('/', '.') for entry in modules}
    corpus = set(names.values())
    selected = {names[entry[1]] for entry in modules[:prefix]}
    for entry in modules[:prefix]:
        module = names[entry[1]]
        text = (out / entry[1]).read_text()
        found = set(IMPORT.findall(text)) | set(WIRED)
        if "NoImplicitPrelude" not in entry[3].split(':') and "NoImplicitPrelude" not in text:
            found.add("Prelude")
        if any(dependency in corpus and dependency not in selected for dependency in found - {module}):
            return False
    return True


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workloads', type=Path)
    p.add_argument('--core', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--parser-script', type=Path, required=True)
    p.add_argument('packages', type=Path, nargs='+')
    a = p.parse_args()
    print(f"frontend corpus: {assemble(a.packages, a.core, a.out, a.parser_script)} modules")

    if a.workloads:
        write_workloads(a.workloads, a.out)
