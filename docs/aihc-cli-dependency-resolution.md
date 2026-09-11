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

## The Wasm backend

Wasm configurations build the benchmark package the same way the native ones
do. `ghc-9.14.1-wasm-*` runs `compile_with_cabal.py` with the `ghc-wasm-meta`
cross-compiler (cabal-install cross-compiles with `--with-compiler` pointing
at `ghc-9.14.1-wasm` and the matching `ghc-pkg`/`hsc2hs` siblings the flake
exports next to it), and `aihc-wasm-semispace-*` runs `compile_with_aihc.py`
with `--target wasm32-wasip3`, so `install` builds `snappy-hs` for the Wasm
store before `build-exe` links it. Both were verified to compile and run
`snappy-roundtrip` end to end.

The freeze file only pins Hackage dependencies for the build: it was written
by `cabal freeze` under one GHC and therefore also lists that GHC's boot
libraries, which `compile_with_cabal.py` drops for whatever packages the
chosen compiler's global database already provides. Without that, GHC 9.14
(base 4.22) could never satisfy a `base ==4.21.2.0` pin taken from 9.12.
