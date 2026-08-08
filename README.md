# AIHC benchmarks

Historical runtime benchmarks for every first-parent commit on
[`ai-haskell-compiler/aihc`](https://github.com/ai-haskell-compiler/aihc).

<!-- AUTO-GENERATED: START benchmark-summary -->
_No benchmark results have been published yet._
<!-- AUTO-GENERATED: END benchmark-summary -->

The detailed interactive report is published with GitHub Pages. It provides
filters for platform, benchmark, metric, compiler, backend, garbage collector,
and compiler version.

## Running locally

Requirements are Nix, Git, and an AIHC checkout with an `origin/main` remote.

```console
nix run . -- doctor --aihc-repo /path/to/aihc
nix run . -- plan --aihc-repo /path/to/aihc --fetch
nix run . -- run --aihc-repo /path/to/aihc --jobs 8
```

Run one maximally spaced commit with `run`, or continue until every commit has
a terminal result with `run --all`. Compilations within a revision are parallel;
benchmark executions are sequential.

GHC configurations use `-O2`. AIHC currently has no numeric optimization flag,
so the corresponding profile uses AIHC's default optimizing pipeline.
GHC is measured with its native and LLVM backends. Wasm is an AIHC-only result,
so it is reported as an absolute wall time rather than as an AIHC/GHC ratio.

Results and resumable state are stored in `.state/benchmarks.sqlite3`. A failed
historical compiler is terminal until its record is deliberately removed:

```console
nix run . -- forget <commit> --aihc-repo /path/to/aihc
```

## Measurement

The runner measures complete process invocations, including native startup and
Wasmtime startup. It records wall time and peak RSS for run buckets of 1, 2, 4,
8, 16, 32, and 64 processes, stopping when adjacent bucket means are within 1%.
All raw samples and the stopping reason are retained.

## Publishing

Publishing requires `R2_ACCOUNT_ID`, `AWS_ACCESS_KEY_ID`, and
`AWS_SECRET_ACCESS_KEY`. Set `R2_PUBLIC_BASE_URL` to the bucket's public custom
domain; `R2_BUCKET` can override the configured bucket name. The bucket is publicly readable but the credentials
granting write access remain local to the publisher.

See [docs/architecture.md](docs/architecture.md) for the data contract and
publication lifecycle.
