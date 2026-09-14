# AIHC benchmarks

Historical runtime benchmarks for every first-parent commit on
[`ai-haskell-compiler/aihc`](https://github.com/ai-haskell-compiler/aihc)
since `aihc_since` in `benchmark.json` (2026-09-12); earlier commits predate
the current `aihc` command line and are never planned.

Results are published at [perf.aihc.app](https://perf.aihc.app): an overview
of AIHC against GHC for the newest measured commit, a timeline per benchmark
and metric, a page per commit, and the coverage of the commit history.

## Running locally

Requirements are Nix and Git. The GHC side resolves its dependencies against a
local Hackage package list, which `doctor` checks for and `cabal update`
populates once per machine.

```console
nix develop --command cabal update
nix run . -- doctor
nix run . -- plan --fetch
nix run . -- run --jobs 8
```

Without `--aihc-repo` the commands use
[`ai-haskell-compiler/aihc`](https://github.com/ai-haskell-compiler/aihc),
cloned once into `.cache/aihc` and reused afterwards. Pass `--aihc-repo` (or
set `AIHC_REPOSITORY`) to benchmark another clone URL, or a local checkout
with an `origin/main` remote:

```console
nix run . -- run --aihc-repo /path/to/aihc --jobs 8
```

`doctor` prints the machine ID that keys this machine's results. It is derived
from the CPU model and a hashed hardware identifier and frozen in
`.state/machine.json`; pass `--machine <id>` to override it.

Run one commit with `run`, or continue until every commit has a terminal
result with `run --all`. The planner measures an unmeasured `HEAD` first, then
the newest 20 commits, then bisects the gap with the highest score, where a
gap scores by its width, by the change observed between its measured
endpoints, and by recency. Commits that leave the compiler-relevant paths
untouched inherit their neighbour's result instead of being measured.
`plan` reports measured and inherited coverage separately. Compilations
within a revision are parallel; benchmark executions are sequential.

Every configuration is measured in four profiles: `O0`, `O1`, `O2` and `Os`.
AIHC passes the matching `-O` flag to `aihc build`, where `-O2` and `-Os` also
compile the whole program at once. GHC has no size level, so its `Os` profile
builds with `-O1`. Every artifact is stripped before its size is recorded: `llvm-strip` for native binaries and
`wasm-tools strip --all` for Wasm, which also handles the component AIHC
emits. Stripping happens after the timed compile. GHC is measured with its
native and LLVM backends, and with the `ghc-wasm-meta` cross-compiler for
Wasm. GHC targets `wasm32-wasi` while AIHC targets `wasm32-wasip3`; both run
under Wasmtime, so the Wasm ratio includes the difference between the two host
interfaces' startup costs. Every GHC configuration, Wasm included, builds the
benchmark's Cabal package with `cabal build` against the flake's toolchain
(`ghc-<version>` plus its `ghc-pkg`/`hsc2hs` siblings), so Hackage
dependencies are resolved and compiled inside the timed step; a benchmark's
`cabal.project.freeze` pins those dependencies while boot libraries come from
whichever GHC is under test.

AIHC builds the same package directory with `aihc build`, which reads the
`.cabal` file and resolves its dependencies itself rather than the freeze
file. Non-boot Hackage dependencies therefore carry an exact `==` bound in the
benchmark's `.cabal` (matching the freeze pin), so both toolchains compile the
same version of, say, `snappy-hs`. Boot libraries are deliberately left
unbounded: GHC ships its own and the versions differ per release, so pinning
them in the `.cabal` would break every toolchain but the one the freeze file
was written under.

Before the timed compile the runner installs `aihc-base` and the dependencies
GHC ships as boot libraries into a per-commit store, once per target and
optimization level, so both toolchains pay for the same work inside the timed
step: the benchmark and its non-boot Hackage dependencies.

Results and resumable state are stored in `.state/benchmarks.sqlite3`. A failed
historical compiler is terminal until its record is deliberately removed:

```console
nix run . -- forget <commit>
```

## Comparing two builds

```console
nix run . -- compare HEAD~5 HEAD
nix run . -- compare main --worktree /path/to/aihc \
  --bench integer-fibonacci-v1 --rounds 20
```

`compare` builds both compilers, compiles the selected benchmarks with each, and
runs them in interleaved A, B, A, B rounds, after one discarded warm-up round,
so machine drift affects both sides equally. It prints medians, the change of B
relative to A, and a bootstrap 95% interval on that change; `--markdown` formats
the table for a pull request. Results are stored locally in `adhoc_runs` and
never uploaded.

## Measurement

The runner measures complete process invocations, including native startup and
Wasmtime startup. It records wall time, CPU time and peak RSS for run buckets
of 1, 2, 4, 8, 16, 32, and 64 processes, stopping when adjacent bucket means
are within 1%. Peak heap, bytes allocated, GC count and GC time come from GHC's
`+RTS -t` output and from the `AIHC_RTS_STATS` hook once the AIHC runtime has
it. Compile time and artifact size are recorded per compilation. Every
configuration is compiled from scratch -- the build directory and any previous
artifact are removed first -- so compile time always describes a full compile
rather than an incremental no-op. Both compilers run with `+RTS -N -RTS` and so
use every core, which makes compile time a multithreaded measurement: run with
`--jobs 1` to measure it without configurations competing for the machine. All
raw samples and the stopping reason are retained.

## Uploading

Results are served by a Cloudflare Worker at
[perf.aihc.app](https://perf.aihc.app), whose source and static pages live in
`web/`. Uploading needs nothing beyond a Cloudflare login on the machine:

```console
wrangler login
nix run . -- upload
nix run . -- run --all --upload
```

The uploader writes envelopes to the R2 bucket and index rows to the D1
database through `wrangler`, so whoever can log in to the Cloudflare account
can upload, and the Worker itself is read-only. `upload` pushes the commit
list and every run not yet acknowledged; `run --upload` does the same after
each commit. Uploads are idempotent. Machines appear on the site under their
derived id, such as `apple-m4-pro-542f1e`.

## Deploying the Worker

```console
cd web && npm ci && npm run check && npm test
npm run migrate && npm run deploy
```

Pushes to `main` that touch `web/` deploy through GitHub Actions using the
`CLOUDFLARE_API_TOKEN` repository secret, which needs the Workers Scripts and
D1 edit permissions.

See [docs/architecture.md](docs/architecture.md) for the data contract and
[docs/design.md](docs/design.md) for the Worker API.
