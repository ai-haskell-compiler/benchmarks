# fast.aihc.app design

This document records the target design for the next iteration of the
benchmark suite. It supersedes the publication and hosting sections of
[architecture.md](architecture.md); the measurement invariants there still
apply unless stated otherwise.

## Decisions

- fast.aihc.app is one Cloudflare Worker with static assets, a D1 index, and
  the existing R2 bucket for raw envelopes. GitHub Pages and the results-update
  PR loop are removed.
- The CLI has three modes over one engine: `compare` (ad-hoc, local only),
  `run` (one commit), and `run --all` (overnight). Ad-hoc results are never
  uploaded.
- Two optimization profiles: `O0` (unoptimized) and `O2`. Both are measured for
  every commit.
- Metrics per invocation: wall time, CPU time (user + system), peak RSS, peak
  heap, bytes allocated, GC count. Per compile: compile wall time and artifact
  size. Peak heap and allocation counters come from a runtime stats hook that
  AIHC will gain; older commits report them as unavailable.
- Time series are keyed by a machine ID derived from the CPU model and a
  hashed hardware identifier, not by an environment fingerprint.

## Repository layout

```text
benchmarks/        Haskell benchmark programs (unchanged)
benchmark.json     suite definition (unchanged role)
aihc_bench/        Python CLI (unchanged package name)
web/               Cloudflare Worker: wrangler.jsonc, src/, public/, migrations/
schema/            JSON Schema for the envelope, shared by CLI tests and Worker
docs/
```

The Worker deploys from GitHub Actions on push to `main` with `wrangler deploy`.
The only CI secret is a Cloudflare API token scoped to Workers and D1.

## Machine identity

`environment_record` currently hashes hostname, OS release, OS version and OS
build into the environment ID. Every OS update therefore starts a new series.

Replace it with two fields:

- `machine_id`: derived automatically, stable across OS updates, and the
  series key. It is a readable slug plus a short hardware hash, for example
  `apple-m4-max-3f9a1c` or `amd-ryzen-9-7950x-b81e07`.
- `environment`: the fingerprint, recorded per run and stored alongside
  results. Add `cpu_brand`, `cpu_cores`, and `memory_bytes`. Its hash is
  `environment_id`. The site draws a marker where a machine's
  `environment_id` changes.

### Deriving the machine ID

1. Read the CPU brand string: `sysctl -n machdep.cpu.brand_string` on Darwin,
   `model name` from `/proc/cpuinfo` on Linux.
2. Lowercase it, drop the tokens `processor`, `cpu`, `(r)`, `(tm)`, `core`,
   `with`, `radeon`, `graphics`, and any token matching `\d+-core`, then keep
   the first three remaining tokens joined with `-`.
3. Read a hardware identifier: `IOPlatformUUID` from
   `ioreg -d2 -c IOPlatformExpertDevice` on Darwin; `/etc/machine-id` on Linux,
   preferring `/sys/class/dmi/id/product_uuid` when it is readable.
4. Append the first six hex characters of the SHA-256 of that identifier. The
   raw identifier is never stored or uploaded.
5. Write the result to `.state/machine.json` on first use. Later runs read the
   file and never re-derive, so a change to the slug rules cannot fork a
   series. `doctor` prints the ID and its derivation.

When no hardware identifier is readable, the suffix falls back to a hash of
the hostname, `doctor` warns, and `--machine <id>` can override the ID.
Two machines of the same model differ in the suffix only. A Linux reinstall
changes `/etc/machine-id` and therefore starts a new series, which matches the
fact that it is a new environment.

The D1 `machines` table carries a mutable `display_name` for the site, so a
human label can be attached without touching the series key.

The upload token is issued per `machine_id`, so a token cannot write another
machine's series.

## Data model

### Envelope

The versioned envelope is kept. Changes for `schema_version: 2`:

- Add `machine_id` at the top level and `environment_id` inside `environment`.
- Add `cpu_time_ns` to every sample (from `ru_utime + ru_stime`).
- Add optional `runtime_stats` to every sample: `peak_heap_bytes`,
  `allocated_bytes`, `gc_count`, `gc_time_ns`. Absent when the runtime did not
  report them.
- Add `compile.wall_time_ns` and `compile.artifact_bytes`.
- Add `metrics[]` entries for `cpu_time`, `peak_heap`, `allocated_bytes`,
  `gc_count`, `compile_time`, `artifact_size`. The estimate for each is the
  median of the stable buckets, except `allocated_bytes` and `gc_count`, which
  are deterministic and assert that all samples agree.
- Change the `wall_time` estimate from the pooled mean of the last two buckets
  to their median. Medians are robust to the occasional stalled invocation and
  make step changes crisper in the timeline.

### D1 schema

```sql
CREATE TABLE machines (
  machine_id   TEXT PRIMARY KEY,
  token_hash   TEXT NOT NULL,
  display_name TEXT,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT
);

CREATE TABLE environments (
  environment_id TEXT PRIMARY KEY,
  machine_id     TEXT NOT NULL REFERENCES machines(machine_id),
  first_seen_at  TEXT NOT NULL,
  record         TEXT NOT NULL   -- JSON fingerprint
);

CREATE TABLE commits (
  sha          TEXT PRIMARY KEY,
  ordinal      INTEGER NOT NULL,
  committed_at TEXT NOT NULL,
  subject      TEXT NOT NULL,
  tree_key     TEXT NOT NULL     -- hash of compiler-relevant paths, see Planner
);
CREATE UNIQUE INDEX commits_ordinal ON commits(ordinal);

CREATE TABLE runs (
  run_id         TEXT PRIMARY KEY,
  machine_id     TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  experiment_id  TEXT NOT NULL,
  commit_sha     TEXT NOT NULL,
  compiler_status TEXT NOT NULL, -- built | unavailable
  created_at     TEXT NOT NULL,
  envelope_key   TEXT NOT NULL   -- R2 object key
);

CREATE TABLE measurements (
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  machine_id    TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  commit_ordinal INTEGER NOT NULL,
  benchmark     TEXT NOT NULL,
  configuration TEXT NOT NULL,
  metric        TEXT NOT NULL,
  status        TEXT NOT NULL,   -- converged | nonconverged | unavailable | failed
  estimate      REAL,
  sample_count  INTEGER,
  PRIMARY KEY (machine_id, experiment_id, commit_ordinal,
               benchmark, configuration, metric)
);
CREATE INDEX measurements_series
  ON measurements(machine_id, experiment_id, benchmark, configuration, metric, commit_ordinal);
```

Raw samples stay in the R2 envelope; the index holds only estimates. The
local SQLite database adopts the same `runs` and `measurements` tables plus an
`uploaded_at` column on `runs`, so upload is "push runs where `uploaded_at` is
null" and is idempotent on `run_id`.

Ad-hoc `compare` results are stored in a separate local table
(`adhoc_runs`) that the uploader never reads.

## Runtime stats hook

AIHC gains an environment variable read by both the native and Wasm runtimes:

```text
AIHC_RTS_STATS=<path>
```

On normal exit the runtime writes one JSON object to `<path>`:

```json
{
  "schema": 1,
  "peak_heap_bytes": 0,
  "allocated_bytes": 0,
  "gc_count": 0,
  "gc_time_ns": 0
}
```

`peak_heap_bytes` is the maximum live heap after any collection plus the
allocation high-water mark between collections, so it is comparable to GHC's
`max_live_bytes`. Under Wasm the file is written through WASI, so the benchmark
runner passes `--dir` for the stats directory.

GHC baselines use `+RTS -t<path> --machine-readable -RTS`, which requires
`-rtsopts` at compile time. The runner maps `max_live_bytes`, `allocated_bytes`,
`num_GCs` and `GC_cpu_seconds` onto the same fields.

The runner probes for the hook with `aihc --help`, the same way it probes for
`prepare-runtime`. Commits without the hook record `peak_heap`,
`allocated_bytes` and `gc_count` with status `unavailable`.

## Configurations

`optimization` is already a configuration dimension. `benchmark.json` gains
`O0` variants of every configuration. GHC passes `-O0`; AIHC passes `-O0` once
the flag exists and is probed like the stats hook. Until then AIHC `O0`
configurations are recorded as `unavailable`, which keeps the experiment ID
honest rather than silently measuring the default pipeline twice.

GHC baselines are split into two roles:

- **Canary:** `ghc-9.14.1-native-O2` and `ghc-9.14.1-native-O0` run in every
  session, immediately before the AIHC configurations. They anchor the
  AIHC/GHC ratio against same-session machine state.
- **Full matrix:** the remaining GHC configurations run when the machine has no
  result for the current `environment_id`, or when the newest result is older
  than 24 hours. They are stored against the same commit ordinal as the session
  that triggered them.

## CLI

```text
aihc-bench doctor    [--machine <id>]          print or override machine ID
aihc-bench plan      [--fetch]                 show coverage and next commit
aihc-bench run       [--all] [--until HH:MM] [--upload] [--jobs N]
aihc-bench compare   <A> <B> [--worktree PATH] [--bench ID...] [--profile O0|O2]
                     [--config ID...] [--rounds N] [--markdown]
aihc-bench upload    [--dry-run]
aihc-bench forget    <commit>
aihc-bench token     --machine <id>            print the upload token (admin)
```

### run

`run --all` keeps its current loop. Additions:

- `--until HH:MM` stops before starting a commit that would not finish by the
  deadline, using the median duration of the last five commits.
- `--upload` calls the uploader after each commit rather than at the end.
- Between commits the loop refetches `origin/main`, and refuses to start when
  the one-minute load average exceeds `cpu_cores / 2` or macOS reports thermal
  pressure above nominal. It waits and retries rather than exiting.

### compare

`compare` reuses the compile and measure pipeline with a different schedule:

1. Build both compilers in parallel; `--worktree` builds the dirty working copy
   as side B and labels it `worktree`.
2. Compile every selected benchmark with both.
3. Run in interleaved rounds A, B, A, B for `--rounds` (default 10) rounds per
   benchmark. Interleaving cancels slow drift from thermal state.
4. Report the median for each metric on each side, the ratio B/A, and a 95%
   bootstrap interval on the ratio. Mark a difference as significant only when
   the interval excludes 1.0.

Output is a table; `--markdown` emits the same table for pull request
comments. Results are stored in `adhoc_runs` and are never uploaded.

## Planner

The planner keeps "unmeasured HEAD first" and replaces pure spacing with a
scored selection over three stages.

### Stage 0: tree keys

Each commit gets `tree_key = git rev-parse <sha>:<path>` combined over the
compiler-relevant paths, currently `bin/aihc`, `core-libs`, `components` and
`flake.lock`. A commit whose `tree_key` equals its first parent's inherits the
parent's results for every configuration, recorded with status `inherited` and
the parent's `run_id`. Inherited results count as measured for coverage and are
never selected for measurement. Docs, CI and test-only commits therefore cost
nothing.

### Stage 1: recent warmup

While any of the newest 20 first-parent commits is unmeasured, select the
newest unmeasured one.

### Stage 2: scored gaps

Consider every maximal run of unmeasured commits between two measured commits
(a gap). For each gap compute

```text
signal  = max over (benchmark, configuration, metric in {wall_time, allocated_bytes})
          of |log(estimate_right / estimate_left)|
score   = width * (1 + 8 * signal) * (1 + recency)
recency = 1 - ordinal_of_gap_midpoint / ordinal_of_head        (in [0, 1])
```

Select the midpoint of the highest-scoring gap. Ties break toward the newer
gap. A gap with no signal is still worth `width`, so coverage improves
everywhere while regressions are localized first. `allocated_bytes` is included
because it is noise-free and bisects reliably even on a loaded machine.

Gaps whose endpoints have status `unavailable` on one side carry `signal = 0`.

`plan` prints the top five gaps with their scores so the choice is auditable.

## Worker API

All responses are JSON with `Cache-Control: public, max-age=300` except upload.

```text
POST /api/upload
  Authorization: Bearer <machine token>
  Body: gzip envelope
  Writes raw/v2/<machine>/<commit>/<run_id>.json.gz to R2, upserts
  commits/environments/runs/measurements. Returns {run_id, inserted}.

GET  /api/overview
  For each machine: latest measured HEAD, geometric-mean AIHC/GHC ratio per
  benchmark per profile, coverage fraction, last upload time.

GET  /api/series?machine=&benchmark=&metric=&profile=
  Arrays of {ordinal, sha, estimate, status} per configuration, plus
  environment change ordinals.

GET  /api/commit/<sha>
  Every measurement for the commit across machines, with delta to the
  first parent and links to the R2 envelopes.

GET  /api/compare?a=<sha>&b=<sha>&machine=
  Per benchmark, configuration and metric: both estimates, ratio, and whether
  either side is inherited or unavailable.

GET  /api/coverage?machine=
  Bitmap of measured, inherited, unavailable and unmeasured ordinals.

GET  /api/commits
  Ordinal, sha, subject and date, for the commit picker.
```

`commits` is refreshed by the uploader, which sends the first-parent list it
already has whenever it uploads. The Worker never runs Git.

## Site

Five pages, vanilla JavaScript, uPlot for charts.

- **Overview** (`/`): the `/api/overview` scorecard. One card per machine
  with `cpu_brand`, a ratio per benchmark and profile, and a 90-commit
  sparkline of the geometric mean.
- **Timeline** (`/timeline`): x-axis is commit ordinal, one line per
  configuration, machine and profile toggles, log scale toggle, ratio to the
  canary GHC configuration as the default view with absolute values as an
  option. Environment changes are vertical markers. Any step larger than 5%
  between adjacent measured commits gets a marker linking to the commit page.
- **Commit** (`/commit/<sha>`): the `/api/commit` table with deltas to the
  parent, colored by significance, and links to GitHub and to the raw
  envelopes.
- **Compare** (`/compare?a=&b=`): the same table as the CLI, computed from
  `/api/compare`.
- **Coverage** (`/coverage`): one row per machine, one cell per commit,
  colored by status. It doubles as the planner's progress display.

Every filter state is reflected in the URL so views can be linked.

## Rollout

1. **Metrics and identity.** Add `machine_id`, CPU time, compile metrics,
   `schema_version: 2`, and the GHC RTS stats mapping. Land the AIHC runtime
   hook and `-O0` flag in the AIHC repository. Add `O0` configurations. This
   starts a new experiment ID, so it should land before any long overnight run.
2. **Planner.** Tree keys, inherited results, warmup and scored gaps. Testable
   entirely offline against the existing planner tests.
3. **Worker.** D1 migrations, upload endpoint, read endpoints, `wrangler deploy`
   workflow, `fast.aihc.app` custom domain. Local uploader with `uploaded_at`.
4. **Site.** Overview and timeline first, then commit, compare and coverage.
5. **Removal.** Delete `pages.yml`, `results-update.yml`, `scripts/build-site.py`,
   the catalog builder and the README summary generator once the site is live.
6. **compare.** Last, because it depends on nothing above except the shared
   measurement engine and the `O0` configurations.
