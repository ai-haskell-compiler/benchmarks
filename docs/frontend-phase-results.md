# Historical Show-based frontend phase measurements

These measurements use the former `Show`-based checksum. See [the structural-digest results](frontend-structural-results.md) for the updated AIHC/GHC comparison.

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

## Full aihc-prim only

This diagnostic includes all **27 exposed modules** of the pinned `aihc-prim` library, in 14 SCCs (one cyclic), with no `aihc-base`. The earlier Prelude/core workload includes only the 20 primitive modules needed by its import closure, so the primitive inputs differ. Sources are copied byte-for-byte from the same pinned commit.

Isolated diagnostic copies of both executables replace `selected <- closure Set.empty ("aihc-base" : roots)` with `selected <- closure Set.empty roots` in `Corpus.hs`. They run with `--packages aihc-prim --repeat 1`. The configured benchmarks still require the full real Prelude. Build flags, compiler binary and phase boundaries match the earlier measurements. GHC 9.14.1 `-O2` independently reproduces both checksums. Means below use five fresh AIHC processes after one discarded warmup, in seconds.

| Phase | Resolver benchmark | Type-checker benchmark |
| --- | ---: | ---: |
| Source I/O and decoding | 0.027664 | 0.022869 |
| Parsing | 0.507015 | 0.511229 |
| Dependency/SCC planning | 0.000325 | 0.000268 |
| Export/scope collection | 0.016029 | 0.011951 |
| Name resolution | 0.115715 | 0.117037 |
| Type checking | — | 4.116865 |
| Interface merging | — | 0.004756 |
| Checksumming (traversal, Show, FNV) | 0.187535 | 3.375812 |
| Unattributed bookkeeping | 0.000147 | 0.000245 |
| Sweep total | 0.854430 | 8.161030 |
| Whole process | 0.861658 | 8.177767 |
| Whole process, timing disabled | 0.851380 | 8.081712 |

Result tallies: 22,309 resolution annotations; 3,288 inferred binding results. The JSON records the diagnostic source change, source/corpus hashes, executable hashes, exact commands and raw samples.
