# AIHC frontend phase measurements

Measured 2026-10-19 on Apple M4 Pro / macOS ARM64, with AIHC `-O2`, native `apple-arm64`, unstripped executables. Every sweep processes the full real Prelude from pinned `aihc-base` sources, in dependency-first SCC order. Hackage `aihc-parser-5.0.0.0` remains unchanged.

Each entry is an arithmetic mean of five fresh processes after one discarded warmup; units are seconds. The core workload is `aihc-prim` + `aihc-base` (139 modules, 98 SCCs, two cyclic). The configured `O2/native` workload adds `colour` + `split` (156 modules, 115 SCCs, two cyclic).

## Configured O2/native workload

| Phase | Resolver benchmark | Type-checker benchmark |
| --- | ---: | ---: |
| Source I/O and decoding | 0.056205 | 0.060743 |
| Parsing | 0.884901 | 0.945965 |
| Dependency/SCC planning | 0.002913 | 0.006030 |
| Export/scope collection | 0.049515 | 0.052509 |
| Name resolution | 0.287903 | 0.305942 |
| Type checking | — | 7.557381 |
| Interface merging | — | 0.300163 |
| Checksumming (traversal, Show, FNV) | 0.645834 | 3.607144 |
| Unattributed bookkeeping | 0.000496 | 0.002275 |
| Sweep total | 1.927768 | 12.838151 |
| Whole process | 1.933493 | 12.853194 |
| Whole process, timing disabled | 1.937483 | 12.612060 |

## Prelude/core workload

| Phase | Resolver benchmark | Type-checker benchmark |
| --- | ---: | ---: |
| Source I/O and decoding | 0.053898 | 0.067966 |
| Parsing | 0.843009 | 0.950970 |
| Dependency/SCC planning | 0.002382 | 0.006323 |
| Export/scope collection | 0.054433 | 0.054770 |
| Name resolution | 0.264806 | 0.303419 |
| Type checking | — | 7.273924 |
| Interface merging | — | 0.287779 |
| Checksumming (traversal, Show, FNV) | 0.593937 | 3.576002 |
| Unattributed bookkeeping | 0.000439 | 0.003090 |
| Sweep total | 1.812903 | 12.524243 |
| Whole process | 1.818488 | 12.542087 |
| Whole process, timing disabled | 1.791933 | 11.611913 |

Resolution includes forcing dynamic resolution payloads; type checking includes materializing inferred types and new interface facts. Checksumming covers traversal and formatting/hashing of materialized results. The original AST retention across each package is preserved. GC pauses are charged to whichever phase is active. The phase intervals are disjoint; their sum plus bookkeeping equals each sweep total. Startup, reporting and shutdown account for the difference between sweep and process times.

These are local desktop measurements with concurrent user activity, not isolated CPU profiles. Timed and timing-disabled groups were sequential; their difference includes runtime variation as well as timing overhead. Explicit semantic result materialization also adds traversal work compared with the earlier harness. All measured runs match the existing reference checksums.

The compiler commit is unavailable; the compiler binary SHA-256 used for these builds is `69fcee510c02e03eb902e73cc8a077a06e70758a1e87f21d858da8cd16f39a80`. Library/core sources remain pinned to `6ceb5ca6b5e6308fb48ee61a557aa6274aa54c2d`. Exact executable hashes, commands, call counts and raw samples are in [frontend-phase-results.json](frontend-phase-results.json). See [the phase measurement command and boundaries](frontend-benchmarks.md#measuring-individual-phases) to reproduce.
