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
- Raw result objects are immutable once uploaded.

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

Every configuration carries an `optimization` profile: `O0`, `O1`, `O2` or
`Os`. GHC receives the matching flag, except that `Os` builds with `-O1`
because GHC has no size level. AIHC `O2` uses the compiler's default
optimizing pipeline. AIHC `O0` configurations require the `optimization-flag`
capability (`build-exe --help` accepts `-O` at all); `O1` and `Os` require
`optimization-O1` and `optimization-Os`, probed from the levels that help text
lists. A commit lacking the capability records the configuration as
unavailable, so the history stays honest until the flag exists.

After a successful compile the runner strips the artifact in place before
recording `artifact_size`: `llvm-strip` for native binaries, `wasm-tools strip
--all` for Wasm. The Wasm tool is the only one of the three in the flake that
parses the component AIHC emits, and `--all` removes the `name` and
`producers` sections that hold nearly all strippable bytes in both the AIHC
component and the GHC module. Stripping runs outside the timed compile, so
`compile_time` still measures the compiler alone.

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

The native and LLVM GHC configurations use the default GMP `ghc-bignum`
backend. The Wasm GHC is `native-bignum` by construction, since there is no
GMP for `wasm32`, so Wasm ratios compare against a native-bignum GHC while the
other backends compare against GMP.

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

Results are uploaded with `wrangler`, authorized by the machine's own
`wrangler login`. The uploader stores the gzip envelope in R2 under
`raw/v2/<machine>/<commit>/<run_id>.json.gz` with `wrangler r2 object put`,
then upserts the commit history and inserts the run and its metric estimates
into D1 with `wrangler d1 execute`. Inherited runs are indexed under
`<source run_id>~<sha12>` and point at the source envelope instead of storing
a copy. The Worker in `web/` only reads. Inserts use `INSERT OR IGNORE` on
their primary keys, and the local database records `uploaded_at` only after
both commands succeeded, so an interrupted upload resumes where it stopped.

Read endpoints (`/api/overview`, `/api/series`, `/api/commit/<sha>`,
`/api/coverage`, `/api/commits`, `/api/machines`, `/api/experiments`) are
public, cached for one minute, and default to the experiment of the most recent
upload. `/api/raw/<key>` streams envelopes from R2. The D1 schema is
`web/migrations/0001_init.sql`.

The overview is materialized: the Worker stores the computed JSON in R2 under
`cache/overview/v1.json` and serves the front page from that copy without
touching D1, which is slow from a cold Worker. A copy older than a minute is
served as is and recomputed in the background. After an upload the uploader
requests `/api/overview?refresh=1`, which recomputes synchronously so the next
visitor sees the new results; a refresh request is ignored while the copy is
younger than ten seconds.

## Platform independence

Apple Arm64 and Linux AMD64 keep separate local databases and publish separate
series. Publishing merges catalog entries by experiment, platform, benchmark,
and metric, so one platform cannot replace the other. Absolute values are never
combined across environment IDs.
