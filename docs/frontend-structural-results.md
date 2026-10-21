# Frontend benchmarks with structural checksums

Measured 2026-10-19 on Apple M4 Pro / macOS ARM64. Both compilers use `-O2` and the native backend (GHC 9.14.1 `-fasm`, AIHC `apple-arm64`); executables are unstripped. Libraries/core sources remain pinned to `6ceb5ca6b5e6308fb48ee61a557aa6274aa54c2d`; `aihc-parser-5.0.0.0` remains from Hackage.

Each number is the arithmetic mean of five fresh processes after one discarded warmup, in seconds. Timing-disabled controls are a separate sequential group of five processes. Measurements are local wall times with ordinary desktop activity; GC belongs to the active phase. Every run passes the same structural-digest reference for both compilers.

The new word-wise digest visits resolution records directly and recursively visits complete inferred binding types, including invisible kinds, predicates and literal payloads. It constructs no `Show` strings. The pre-existing explicit forcing of ASTs, all resolution payloads, complete inferred binding types and new interface facts is retained. Original package-level AST retention is preserved. The `checksum-tests` suite checks semantic distinctions and forces nested lazy payloads; exhaustive constructor matching is enforced at compilation.

## Configured O2/native: full Prelude + colour + split

156 modules, four packages, 115 SCCs (two cyclic). The full real Prelude and its dependency closure are processed during every sweep.

| Phase | AIHC resolver | GHC resolver | AIHC TC | GHC TC |
| --- | ---: | ---: | ---: | ---: |
| Source I/O and decoding | 0.060314 | 0.010254 | 0.060383 | 0.011202 |
| Parsing | 0.930078 | 0.305190 | 0.911360 | 0.336118 |
| Dependency/SCC planning | 0.002172 | 0.001156 | 0.005598 | 0.001233 |
| Export collection | 0.053869 | 0.031225 | 0.051417 | 0.029342 |
| Name resolution | 0.306112 | 0.135717 | 0.302824 | 0.123270 |
| Type checking | — | — | 7.543610 | 2.478767 |
| Interface merging | — | — | 0.303403 | 0.079797 |
| Structural checksumming | 0.084911 | 0.039950 | 0.357160 | 0.229053 |
| Bookkeeping | 0.000841 | 0.000214 | 0.001592 | 0.000570 |
| Sweep total | 1.438297 | 0.523707 | 9.537346 | 3.289353 |
| Whole process, timings enabled | 1.444534 | 0.538788 | 9.550154 | 3.315600 |
| Whole process, timings disabled | 1.436037 | 0.531113 | 9.500984 | 3.322919 |

## Full aihc-prim only

All 27 exposed modules in the pinned primitive library, 14 SCCs (one cyclic), without aihc-base. `--exact-packages aihc-prim` selects this diagnostic; configured workloads and ordinary `--packages` still automatically include the full real Prelude. The corpus is identical to the earlier full primitive-only measurement, with all source files copied byte-for-byte from the pinned core library.

| Phase | AIHC resolver | GHC resolver | AIHC TC | GHC TC |
| --- | ---: | ---: | ---: | ---: |
| Source I/O and decoding | 0.022026 | 0.002992 | 0.022311 | 0.003303 |
| Parsing | 0.498715 | 0.170722 | 0.502344 | 0.178614 |
| Dependency/SCC planning | 0.000284 | 0.000186 | 0.000297 | 0.000183 |
| Export collection | 0.013494 | 0.006700 | 0.011749 | 0.006761 |
| Name resolution | 0.104629 | 0.033649 | 0.119343 | 0.033770 |
| Type checking | — | — | 3.975740 | 1.328444 |
| Interface merging | — | — | 0.004698 | 0.001157 |
| Structural checksumming | 0.034218 | 0.013318 | 0.315481 | 0.218340 |
| Bookkeeping | 0.000387 | 0.000036 | 0.000221 | 0.000051 |
| Sweep total | 0.673753 | 0.227603 | 4.952185 | 1.770624 |
| Whole process, timings enabled | 0.679850 | 0.245169 | 4.966286 | 1.792298 |
| Whole process, timings disabled | 0.680469 | 0.248400 | 4.980228 | 1.789693 |

## Effect on checksum time in AIHC

The earlier Show-based runs used the same compiler binary and source inputs. These are separate local sample groups, so other phase timings can vary with GC and desktop activity.

| Workload | Benchmark | Previous Show checksum | Structural checksum |
| --- | --- | ---: | ---: |
| Prelude + colour + split | resolve | 0.645834 | 0.084911 |
| Prelude + colour + split | tc | 3.607144 | 0.357160 |
| Full aihc-prim | resolve | 0.187535 | 0.034218 |
| Full aihc-prim | tc | 3.375812 | 0.315481 |

## Reproduction and validation

Use [`scripts/measure-frontend-phases.py`](../scripts/measure-frontend-phases.py) with each compiler's executables, as described in [the workload documentation](frontend-benchmarks.md#measuring-individual-phases). For the full primitive-only corpus, pass `--exact-packages aihc-prim --references docs/frontend-prim-reference.json`; its 27 source paths and hashes are in the [raw result JSON](frontend-structural-results.json). Both measurements use the same binaries; no diagnostic source edits are needed. The standard corpus contains only 20 primitive modules from the Prelude dependency closure, so it cannot reproduce the full 27-module diagnostic.

The pinned AIHC compiler binary SHA-256 is `69fcee510c02e03eb902e73cc8a077a06e70758a1e87f21d858da8cd16f39a80`; its Git revision is unavailable. GHC 9.14.1 rebuilds the non-wired dependencies at `-O2 -fasm`; the compiler's wired-in libraries remain as shipped. Exact executable/compiler hashes, commands, source hashes, per-sample phase ledgers and timing-disabled controls are recorded in the raw JSON.

Validation: all 312 Python tests, both Cabal SCC suites and the new structural-checksum suite pass. GHC validates all 24 configured selectors against the refreshed references, and ordinary primitive package selection still includes Prelude. Every AIHC and GHC sample in both measured input sets matches the same digest. The full runtime backend/profile matrix has not been measured for these sources.
