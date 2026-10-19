#!/usr/bin/env python3
"""Build a closed, offline frontend workload from complete Stackage libraries.

Only the unconditional, preprocessor-free libraries selected in corpus.nix
are supported here. Fail rather than guess at Cabal conditionals. Core sources
come from the same pinned AIHC revision as the frontend libraries. Their Linux
x86_64 variant is used on every host, so the input bytes remain comparable.
"""
import argparse
import importlib.util
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
        pending.extend(IMPORT.findall(text))
        if "NoImplicitPrelude" not in extensions and "NoImplicitPrelude" not in text:
            pending.append("Prelude")
    lines = []
    for (name, module), (source, edition, extensions) in sorted(sources.items()):
        relative = name + "/" + module.replace('.', '/') + ".hs"
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        lines.append('\t'.join([name, relative, edition, ':'.join(extensions)]) + '\n')
    (out / "modules.tsv").write_text(''.join(lines))
    (out / "packages.tsv").write_text(''.join(name + '\t' + ':'.join(deps) + '\n' for name, (deps, _, _) in sorted(metadata.items())))
    (out / "README.txt").write_text("Complete colour and split libraries from Stackage lts-24.58, with their transitive AIHC core source closure. Fixed Linux x86_64 source variant on every benchmark target.\n")
    return len(sources)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--core', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--parser-script', type=Path, required=True)
    p.add_argument('packages', type=Path, nargs='+')
    a = p.parse_args()
    print(f"frontend corpus: {assemble(a.packages, a.core, a.out, a.parser_script)} modules")
