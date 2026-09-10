# Architecture

## Invariants

- The history is the first-parent chain of `origin/main`, including the root.
- Every commit receives one terminal record per experiment and host platform.
- Results are keyed by a machine ID derived from the CPU model and a
  hashed hardware identifier; the environment fingerprint is recorded
  alongside.
- Unavailable compilers are data, not missing data.
- A terminal result is never retried unless its active record is manually forgotten.
- Compilation is parallel, followed by a full barrier, followed by sequential execution.
- Process startup is part of wall time and peak RSS.
- Raw result objects are immutable. Publication changes only catalogs and
  content-addressed views.

## Machine identity

`.state/machine.json` freezes the machine ID on first use. It is a slug of the
CPU brand string plus six hex characters of the SHA-256 of a hardware
identifier (the IOPlatformUUID on Darwin; the DMI product UUID or
`/etc/machine-id` on Linux), for example `apple-m4-max-3f9a1c`. The raw
identifier is never stored. `doctor` prints the ID and its derivation, warns
when only the hostname was available, and `doctor --machine <id>` overrides
and refreezes it. The environment record still carries the OS release, build,
CPU brand, core count and memory; its `id` hashes those descriptive fields but
not the hostname.

## Local state

SQLite stores discovered commits and the active terminal result for each
`(experiment, platform, commit)` key. The experiment ID hashes every semantic
input: benchmark definitions, matrix, optimization profile, and measurement
settings. Local paths and publishing locations do not affect it.

The planner benchmarks an unmeasured `HEAD` first, then fills in the newest
20 commits, then bisects gaps between measured commits. A gap's score is its
width times `1 + 8 * signal` times `1 + recency`, where the signal is the
largest relative change in wall time or allocated bytes between the gap's
measured endpoints. Commits whose compiler-relevant tree matches a measured
commit inherit its result instead of being measured; see
[design.md](design.md) for the tree-key definition.

Every configuration carries an `optimization` profile, `O0` or `O2`. GHC
receives the matching flag. AIHC `O2` uses the compiler's default optimizing
pipeline. AIHC `O0` configurations require the `optimization-flag` capability
and are recorded as unavailable on commits whose `build-exe --help` does not
advertise one, so the history stays honest until the flag exists.

## Toolchains

The flake builds the GHC wrappers once and exports their directory as
`AIHC_BENCH_TOOLCHAINS`; compile templates reference
`{toolchains}/bin/ghc-<version>`. `ghc-9.14.1-wasm` wraps the `ghc-wasm-meta`
cross-compiler, pinned as a flake input, whose programs run under Wasmtime with
`+RTS -t` statistics like the native ones. The runner never invokes `nix run` on
this repository itself: doing so copied the working tree, including `.cache`,
into the Nix store on every compile and raced with the AIHC store preparation
writing there. The toolchain versions are pinned by `flake.lock`, which is part
of the experiment ID.

## Compiler capabilities

The AIHC command line changed over the history, so the runner probes each
commit's `--help` output rather than assuming a shape. The capabilities are
`build-exe`, `compile`, `prepare-runtime`, `install-offline`,
`optimization-flag` and `build-root`. The compile template uses the
`{aihc_build_command}` placeholder, which resolves to `build-exe` when
available and `compile` otherwise. `install --offline` is passed only when
advertised, and `--build-root` gives every cell its own build directory so
parallel compilations do not share the worktree's `.aihc-target`. A configuration
lists the capabilities it needs in `requires`; a missing one records the cell
as `missing_capability:<name>`. The probed map is stored in the envelope as
`aihc_capabilities`.

GHC 9 and newer replace the former `integer-simple` package with the `native`
backend of `ghc-bignum`. The matrix calls this variant `native-bignum` and tests
it as a separately built GHC toolchain alongside the default GMP variant. The
code-generation backend remains an independent dimension, so both variants are
tested with native and LLVM code generation.

For AIHC revisions with the `prepare-runtime` capability, the runner creates a
store scoped to the experiment, platform, and commit. It prepares each selected
target/GC runtime and installs `aihc-base` for all selected targets before the
parallel compilation phase. Older revisions retain their original self-contained
compile path. Runtime preparation and library installation run per target, so
a failure for one target (for example a missing WASI sysroot) is recorded on
that target's AIHC cells only. The flake exports `AIHC_WASM_SYSROOT`, built
from the nixpkgs `wasilibc` the same way AIHC's own flake does.

## Result envelope

Each completed attempt produces a versioned JSON envelope:

```text
schema_version            2
run_id
created_at
experiment_id
platform
machine_id
environment               fingerprint with id, cpu_brand, cpu_cores, memory_bytes
aihc_commit
aihc_capabilities
compiler_status
unavailable_reason
results[]
  benchmark
  configuration
  compiler_family / compiler_version / compiler_variant
  backend / gc / optimization
  compile                 status, wall_time_ns, artifact_bytes, cached
  measurement
    status
    bucket_sizes
    samples[]             wall_time_ns, cpu_time_ns, peak_rss_bytes,
                          peak_heap_bytes, allocated_bytes, gc_count, gc_time_ns
    metrics[]             metric, unit, status, estimate, samples
```

Metrics carry their own name, unit, status, estimate, and samples. The
invocation metrics are `wall_time`, `cpu_time`, `peak_rss`, `peak_heap`,
`allocated_bytes`, `gc_count` and `gc_time`; `compile_time` and
`artifact_size` describe the compilation. A metric the process could not
report has status `unavailable` and a null estimate. `allocated_bytes` and
`gc_count` are expected to be identical across invocations and are marked
`nondeterministic` when they are not.

## Measurement

The runner directly starts the benchmark process and calls `wait4`, measuring
monotonic elapsed time and child `rusage`. `ru_maxrss` is normalized to bytes;
Darwin reports bytes and Linux reports KiB. CPU time is `ru_utime + ru_stime`.

Wall-time bucket sizes are 1, 2, 4, 8, 16, 32, and 64. Adjacent means converge
when their symmetric relative difference is at most 1%. Every published
estimate is the median of the final two wall-time buckets. Failure and
non-convergence preserve all samples.

### Runtime statistics

A configuration's `runtime_stats` names the format of a statistics file the
process writes. The `{stats_file}` and `{stats_dir}` placeholders in run
templates name that file; it is removed before each invocation and read after
a successful one, so an old runtime that writes nothing simply yields
unavailable metrics.

- `ghc`: the program is compiled with `-rtsopts` and run with
  `+RTS -t{stats_file} --machine-readable -RTS`. `max_live_bytes` becomes
  `peak_heap`, `allocated_bytes` and `num_GCs` map directly, and
  `GC_cpu_seconds` becomes `gc_time`.
- `aihc`: the runner sets `AIHC_RTS_STATS={stats_file}` in the environment
  (through `--env` and `--dir` under Wasmtime). The runtime is expected to
  write a schema 1 JSON object with `peak_heap_bytes`, `allocated_bytes`,
  `gc_count` and `gc_time_ns`. See [design.md](design.md) for the contract.

## Publication

Results are uploaded to the Worker in `web/`. `POST /api/upload` verifies the
machine's bearer token, checks that the envelope names the same machine,
stores the gzip envelope in R2 under
`raw/v2/<machine>/<commit>/<run_id>.json.gz`, and indexes the run and its
metric estimates in D1. Inherited runs are indexed under
`<source run_id>~<sha12>` and point at the source envelope instead of storing
a copy. `POST /api/commits` upserts the first-parent history so the Worker
never runs Git. Uploads are idempotent on `run_id`, and the local database
records `uploaded_at` only after the Worker acknowledged the run, so an
interrupted upload resumes where it stopped.

Read endpoints (`/api/overview`, `/api/series`, `/api/commit/<sha>`,
`/api/compare`, `/api/coverage`, `/api/commits`, `/api/machines`,
`/api/experiments`) are public, cached for one minute, and default to the
experiment of the most recent upload. `/api/raw/<key>` streams envelopes from
R2. The D1 schema is `web/migrations/0001_init.sql`.

### Legacy catalog publication

The previous static catalog flow still exists behind `publish` and creates
three kinds of R2 objects:

- `raw/v1/.../*.json.gz`: immutable canonical envelopes.
- `views/v1/<content-hash>.json`: browser-oriented time series.
- `revisions/v1/<content-hash>.json`: terminal revision indexes.

It then uploads `catalog/candidate.json` last with `Cache-Control: no-cache` and
optionally dispatches the results-update workflow. GitHub Actions only reads the
public catalog; it has no R2 write credentials. The workflow regenerates the
README and checked-in site catalog and opens or refreshes one review PR. Pages
deploys only after that PR is merged.

The bucket has anonymous read access. Write credentials exist only in the local
publisher environment. A suitable CORS policy is:

```json
{
  "rules": [
    {
      "allowed": {
        "origins": [
          "https://ai-haskell-compiler.github.io",
          "http://localhost:8000"
        ],
        "methods": ["GET", "HEAD"]
      },
      "maxAgeSeconds": 3600
    }
  ]
}
```

## Platform independence

Apple Arm64 and Linux AMD64 keep separate local databases and publish separate
series. Publishing merges catalog entries by experiment, platform, benchmark,
and metric, so one platform cannot replace the other. Absolute values are never
combined across environment IDs.
