# Architecture

## Invariants

- The history is the first-parent chain of `origin/main`, including the root.
- Every commit receives one terminal record per experiment and host platform.
- Unavailable compilers are data, not missing data.
- A terminal result is never retried unless its active record is manually forgotten.
- Compilation is parallel, followed by a full barrier, followed by sequential execution.
- Process startup is part of wall time and peak RSS.
- Raw result objects are immutable. Publication changes only catalogs and content-addressed views.

## Local state

SQLite stores discovered commits and the active terminal result for each
`(experiment, platform, commit)` key. The experiment ID hashes every semantic
input: benchmark definitions, matrix, optimization profile, and measurement
settings. Local paths and publishing locations do not affect it.

The planner benchmarks an unmeasured `HEAD` first. Every later selection
maximizes its minimum first-parent ordinal distance from a terminal commit,
breaking ties toward the newer revision.

The `O2` experiment profile passes `-O2` to GHC. AIHC does not currently expose
a numeric optimization flag, so its side of the profile uses the compiler's
default optimizing pipeline. Adding an AIHC optimization flag changes the
configuration and therefore creates a new experiment ID.

## Result envelope

Each completed attempt produces a versioned JSON envelope:

```text
schema_version
run_id
created_at
experiment_id
platform
environment
aihc_commit
compiler_status
unavailable_reason
results[]
  benchmark
  configuration
  compiler_family / compiler_version
  backend / gc / optimization
  compile
  measurement
    status
    bucket_sizes
    samples[]
    metrics[]
```

Metrics carry their own name, unit, estimate, and samples, allowing allocations
and other measurements to be added without changing the envelope.

## Measurement

The runner directly starts the benchmark process and calls `wait4`, measuring
monotonic elapsed time and child `rusage`. `ru_maxrss` is normalized to bytes;
Darwin reports bytes and Linux reports KiB.

Wall-time bucket sizes are 1, 2, 4, 8, 16, 32, and 64. Adjacent means converge
when their symmetric relative difference is at most 1%. The published estimate
is the pooled mean of the final two wall-time buckets. Peak RSS uses the median
of those same invocations. Failure and non-convergence preserve all samples.

## Publication

The publisher creates three kinds of R2 objects:

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
