# Handoff: package-directory dependency resolution for AIHC's CLI

## Context

Benchmarks in this repo are self-contained Cabal packages (see
`benchmarks/*/*.cabal` and `cabal.project.freeze`), so that "compile time"
includes resolving and compiling a real dependency graph, not just one
module. The GHC side of the measurement runs a plain `cabal build`
(`aihc_bench/scripts/compile_with_cabal.py`), which already knows how to
read a package's `.cabal`/`cabal.project.freeze` and resolve its own
dependencies.

AIHC's CLI does not have an equivalent yet. `install PACKAGE` (in
`bin/aihc/src/Aihc/Cli/Install.hs`) parses a `.cabal` file and resolves its
dependency graph against Hackage, but it installs a *library*, not an
executable. `build-exe MODULE [--source-dir DIR] [-p CONSTRAINT]...`
produces the timed executable, but takes only a positional module/file path
and manually supplied `-p name==version` constraints — it does not parse a
package directory's own `.cabal` file.

## Current stopgap (in this repo)

`aihc_bench/scripts/compile_with_aihc.py` bridges the gap: it reads the
benchmark's direct `build-depends` from its `.cabal` file, looks up each
name's exact pinned version in the same `cabal.project.freeze` the GHC side
uses (so both toolchains compile identical dependency versions), and passes
them to `build-exe` as explicit `-p name==version` flags. It skips names in
`aihc_bench.freeze.AIHC_IMPLICIT_PACKAGES` (base, ghc-internal, ghc-prim,
system-cxx-std-lib, template-haskell), which `build-exe` already treats as
built in.

This works for the benchmarks in this repo today (all single-executable
packages with straightforward direct dependencies), but it is a duplicate,
partial reimplementation of dependency resolution that already exists in
`install`. It will not handle anything `install`'s real `.cabal` parsing
handles and this regex-based one does not: multiple components, conditional
`build-depends` (`if flag(...)`), version ranges instead of exact pins, etc.

## Requested change

Give `build-exe` (or a new subcommand) the same package-directory awareness
`install` already has: point it at a directory containing a `.cabal` file
and let it resolve that package's own dependency graph, the way `cabal
build` does, instead of requiring the caller to precompute `-p` flags. A
natural shape, given the existing pieces in `tooling/aihc-package-plan` and
`bin/aihc/src/Aihc/Cli/Install.hs`:

```
aihc build-exe --package-dir DIR [--component NAME] --target T --gc GC ...
```

Once that lands, delete `compile_with_aihc.py`'s freeze/`.cabal` parsing
(`aihc_bench/freeze.py`'s `parse_build_depends`/`resolve_dependency_constraints`)
and pass `--package-dir {source}` directly in `benchmark.json`'s AIHC
`compile` templates.

## `-p` requires a prior `install` (fixed in this repo)

`-p,--package CONSTRAINT` on `build-exe` requires the package to already be
*installed* in the store (`install`'s own `--help` confirms `-p` is "an
installed package constraint") — `build-exe` does not fetch or build
anything on its own; this was verified by actually building AIHC and
running it. `compile_with_aihc.py` runs `aihc install` for every
non-implicit dependency (boot-equivalent and non-boot alike) before calling
`build-exe`. Boot-equivalent dependencies are already installed once per
commit by `_prepare_aihc_store`, so re-installing them here is a cheap,
idempotent no-op; non-boot dependencies are genuinely built inside this
step, which is what the runner times.

## Known blocker: `cxx-sources` isn't compiled by `install`

`snappy-roundtrip` originally used the `snappy` Hackage package (FFI
bindings to Google's C++ Snappy library) and hit a real AIHC limitation,
confirmed by actually building AIHC (`nix run .#aihc` in the sibling repo,
commit `9bc0fc8d4`) and running the full install → build-exe sequence by
hand: `aihc install snappy-0.2.0.4` correctly compiled and linked the
package's `cbits/hs_snappy.cpp` C++ shim into `libsnappy.a` (with
`LIBRARY_PATH`/`CPATH` pointed at a real libsnappy — that env var mechanism
does work), but the resulting `build-exe` link failed with
`Undefined symbols for architecture arm64: __hsnappy_GetUncompressedLength,
__hsnappy_MaxCompressedLength, __hsnappy_RawCompress,
__hsnappy_RawUncompress`. Neither the install log nor
`bin/aihc/src/Aihc/Cli/Install.hs` show any `cxx-sources` handling, and no
`core-libs/*.cabal` in the AIHC repo uses that field either — `install`
simply never compiles a package's `cxx-sources`, so **any package with a
`cxx-sources` field can't be built with AIHC today**, independent of this
benchmark suite.

The benchmark now uses `snappy-hs` instead — a pure-Haskell reimplementation
of the Snappy format (depends only on `base`/`bytestring`, no FFI, no C++
shim) — which avoids this gap entirely while still being a real,
non-boot-equivalent Hackage dependency for the compile-time measurement.
Revisit `snappy` (or any other `cxx-sources`-using package) once AIHC's
`install` supports compiling it.

## Scope note: the Wasm backend

Wasm configurations (`ghc-9.14.1-wasm`, `aihc-wasm-semispace-*`) still
compile a single `Main.hs` directly (`{main_file}` in `benchmark.json`, not
`{source}`), not through Cabal/`build-exe`'s dependency resolution. GHC's
Wasm cross-compiler in this repo's `flake.nix` has no bundled
`wasm32-wasi-cabal`, and AIHC's Wasm target has no verified story for
resolving Hackage dependencies for a cross target either. As a result,
`snappy-roundtrip` (the one benchmark with a real dependency) is not
buildable for the Wasm backend — its compile fails there with a normal
"package not found" error, which is expected and out of scope for this
change. Extending dependency-inclusive compilation to Wasm is a separate,
larger effort.
