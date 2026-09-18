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
nix run . -- run
```

Without `--aihc-repo` the commands use
[`ai-haskell-compiler/aihc`](https://github.com/ai-haskell-compiler/aihc),
cloned once into `.cache/aihc` and reused afterwards. Pass `--aihc-repo` (or
set `AIHC_REPOSITORY`) to benchmark another clone URL, or a local checkout
with an `origin/main` remote:

```console
nix run . -- run --aihc-repo /path/to/aihc
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
`plan` reports measured and inherited coverage separately. Compile time is a
published metric, so compilation is as timing-sensitive as execution: both are
sequential and neither shares the machine with anything else the suite does.
Building the compiler itself is not timed and may use the whole machine.

Every configuration is measured in four profiles: `O0`, `O1`, `O2` and `Os`.
AIHC passes the matching `-O` flag to `aihc build`, where `-O2` and `-Os` also
compile the whole program at once. GHC has no size level, so its `Os` profile
builds with `-O1`. Both compilers apply the level to the whole build: on the
GHC side that takes a `package *` stanza in the generated project file, since
cabal's command-line `-O` flag reaches local packages only and would leave the
Hackage dependencies at `-O1`. Every artifact is stripped before its size is recorded: `llvm-strip` for native binaries and
`wasm-tools strip --all` for Wasm, which also handles the component AIHC
emits. Stripping happens after the timed compile. GHC is measured with its
native and LLVM backends, and with the `ghc-wasm-meta` cross-compiler for
Wasm. GHC targets `wasm32-wasi` while AIHC targets `wasm32-wasip3`; both run
under Wasmtime, so the Wasm ratio includes the difference between the two host
interfaces' startup costs. Every GHC configuration, Wasm included, builds the
benchmark's Cabal package with `cabal build` against the flake's toolchain
(`ghc-<version>` plus its `ghc-pkg`/`hsc2hs` siblings) and a private store per
configuration, so Hackage dependencies are resolved and compiled inside the
timed step for every configuration rather than only the first; a benchmark's
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

## The CPP corpus

`aihc-cpp-stackage` measures [`aihc-cpp`](https://github.com/ai-haskell-compiler/aihc-cpp),
the pure Haskell C preprocessor the compiler uses, on every CPP-using module of
a pinned Stackage snapshot. The program is an ordinary benchmark package that
both toolchains compile; its input is a corpus the flake builds:

```console
nix build .#cpp-corpus
cat result/report.txt
```

The corpus is a plain directory: one subdirectory per package holding the
modules that contain preprocessor directives, the `.cabal` file and every file
they `#include`, plus what a real build would add and a bare source tree
lacks. `generated/<package>/cabal_macros.h` carries `MIN_VERSION_` and
`VERSION_` for the package's dependencies at the snapshot's versions, the way
Cabal writes it; `generated/ghcversion.h` and `macros.tsv` carry the
`__GLASGOW_HASKELL__` family for the snapshot's compiler and a fixed
`x86_64`/`linux` platform, so every machine preprocesses the same branches;
`include/` holds stand-ins for headers GHC ships, such as `MachDeps.h`.
`modules.tsv` lists each module with the headers to pre-include and the
directories to search, and `packages.tsv`, `report.txt` and
`unresolved-includes.txt` say what went in and which includes nothing
satisfies (a package-local header only its `configure` script generates, or
one behind a platform conditional the corpus never takes). Because every
decision is written out, a wrong macro or a missing header can be found by
reading the corpus rather than the program.

The timed run sweeps `benchmark.tsv`, an even stride through the corpus up
to 512 KiB of module source (`sampleBytes` in `corpus/cpp/corpus.nix`; zero
selects everything). A GHC build does the whole 75 MB in about two seconds,
but an AIHC build preprocesses around 40 KB/s today, so the sample is what
keeps an AIHC run inside the benchmark's own `process_timeout_seconds`.
`<program> <corpus> --report` sweeps every module and prints each
diagnostic, which is how the stand-in headers and macros were chosen: what
remains is Windows-only branches, headers only a package's `configure`
script writes, and includes of test files that live outside the package.

The snapshot is pinned in `corpus/stackage/lts-24.58.json` with the SHA-256
of every package tarball, so each is a fixed-output fetch and a failure names
the package. The pin also records the versions of the packages the snapshot's compiler
ships, which Stackage lists as `core`, so nothing is compiled or installed to
build the corpus. Bump it with `corpus/stackage/update.py`, which needs only
the `all-cabal-hashes` tarball from nixpkgs; the docstring has the commands. The runner receives the
built corpus through `AIHC_BENCH_CPP_CORPUS`, exported by the flake like the
toolchains, and passes it to the program as its argument (preopened with
`--dir` under Wasmtime). The printed tally -- module count, modules without an
error diagnostic, total output bytes -- is the benchmark's expected output,
so a miscompiled preprocessor fails the run rather than producing a number.
The corpus sources under `corpus/` are part of that benchmark's experiment
identity and of no other.

Before the timed compile the runner installs `aihc-base` and the dependencies
GHC ships as boot libraries into a per-commit store, once per target and
optimization level, so both toolchains pay for the same work inside the timed
step: the benchmark and its non-boot Hackage dependencies.

Before measuring anything, `run` refreshes AIHC's Hackage index with the
compiler at `aihc_ref`. A measured commit that found the index stale would
refresh it with its own code, so whether a historical commit builds would
depend on how old the cache happened to be when it was scheduled -- and a
failure is terminal. Warming it first means every commit in a run resolves
against the same index and none of them performs the refresh. A warming
failure is reported and the run continues against whatever is cached.

Every AIHC number is published as a ratio against GHC, so a benchmark whose
baseline compiler produced nothing is not a partial result but a useless one.
A commit measured that way stops the run with a non-zero exit rather than
recording an AIHC-only result: the attempt stays unfinished, so the commit is
measured again once the machine is fixed and no `forget` is needed.

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
use every core, which makes compile time a multithreaded measurement of one
compiler with the machine to itself. All raw samples and the stopping reason
are retained.

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
can upload, and the Worker itself is read-only. A wrangler call that fails
with an expired OAuth token (`Authentication error [code: 10000]`, which
`wrangler whoami` does not see) or a network fault is retried with a growing
backoff; anything else fails at once. Uploads are idempotent, so a retry
cannot double-write. `upload` pushes the commit
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
