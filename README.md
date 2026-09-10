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

Every configuration is measured in an `O2` and an `O0` profile. AIHC `O2` uses
the default optimizing pipeline; AIHC `O0` is recorded as unavailable until a
commit's `build-exe` advertises an optimization flag. GHC is measured with its
native and LLVM backends. Wasm is an AIHC-only result, so it is reported as an
absolute wall time rather than as an AIHC/GHC ratio.

Results and resumable state are stored in `.state/benchmarks.sqlite3`. A failed
historical compiler is terminal until its record is deliberately removed:

```console
nix run . -- forget <commit> --aihc-repo /path/to/aihc
```

## Measurement

The runner measures complete process invocations, including native startup and
Wasmtime startup. It records wall time, CPU time and peak RSS for run buckets
of 1, 2, 4, 8, 16, 32, and 64 processes, stopping when adjacent bucket means
are within 1%. Peak heap, bytes allocated, GC count and GC time come from GHC's
`+RTS -t` output and from the `AIHC_RTS_STATS` hook once the AIHC runtime has
it. Compile time and artifact size are recorded per compilation. All raw
samples and the stopping reason are retained.

## Uploading

Results are served by a Cloudflare Worker at
[fast.aihc.app](https://fast.aihc.app), whose source lives in `web/`. Each
machine uploads with its own token:

```console
AIHC_BENCH_ADMIN_TOKEN=... nix run . -- register --display-name "My laptop"
nix run . -- upload
nix run . -- run --all --upload
```

`register` stores the issued token in `.state/upload.json`. `upload` pushes
the commit list and every run the Worker has not acknowledged; `run --upload`
does the same after each commit. Uploads are idempotent.

The legacy `publish` command writes the R2 catalog used by the GitHub Pages
site and will be removed once the Worker serves the full site.

## Deploying the Worker

```console
cd web && npm ci && npm run check && npm test
npm run migrate && npm run deploy
```

Pushes to `main` that touch `web/` deploy through GitHub Actions using the
`CLOUDFLARE_API_TOKEN` repository secret. The Worker needs one secret of its
own, `ADMIN_TOKEN`, set with `wrangler secret put ADMIN_TOKEN`; it authorizes
`register` and nothing else.

See [docs/architecture.md](docs/architecture.md) for the data contract and
[docs/design.md](docs/design.md) for the Worker API.
