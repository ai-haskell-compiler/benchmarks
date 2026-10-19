# Name resolution and type inference

`aihc-resolve-bench-v1` and `aihc-tc-bench-v1` exercise the public APIs of
AIHC's name resolver and type checker. Both use `git_dependencies`, pinned to
AIHC commit `6ceb5ca6b5e6308fb48ee61a557aa6274aa54c2d` (2026-10-19).
The resolver selects `components/aihc-resolve`; the type checker selects
that package and `components/aihc-tc`. The library revision is independent
of the compiler revision being measured.

Both depend on **Hackage `aihc-parser-5.0.0.0`**. Their Cabal bounds and freeze
files pin the parser release, and `aihc.lock` records it as `hackage` and
the selected Git libraries as `local` on macOS ARM64, Linux x86-64 and WASI
wasm32. LLVM uses the host platform's plan. Git fetching and source staging
happen before the compilation timer; compilation of the libraries is timed.

## Real package inputs

The flake's `frontend-corpus` output supplies a pinned input pool through
`AIHC_BENCH_FRONTEND_CORPUS`. Each profile/backend selects its package set
from that pool using `workloads.tsv`. It contains the complete
libraries of **colour-2.3.7** (15 modules) and **split-0.2.5.1** (2 modules)
from the existing Stackage **lts-24.58** snapshot. Their tarballs use the
snapshot's SHA-256 pins and share downloads with the parser and CPP corpora.
Test suites and executables are excluded.

These libraries need Prelude and primitive definitions. The corpus includes
their transitive source closure from the real `aihc-base` and `aihc-prim`
libraries at the same pinned AIHC commit, fetched with a fixed output hash.
It uses the Linux x86-64 source variant on every benchmark target so the
source bytes are identical across configurations. These are workload inputs;
the executable itself still builds against the measured compiler's libraries.
No synthetic declarations or precomputed semantic interfaces are used.

`corpus/frontend/build.py` deliberately supports only the two unconditional,
preprocessor-free libraries selected here. It rejects missing modules,
unknown imports, CPP, conditional/common settings and unexpected package
dependencies. It follows their imports into the core libraries, including
implicit Prelude and the compiler's wired type modules. Adding more complex
libraries will require Cabal component finalization and preprocessing; the
builder must not silently guess those settings.

The pool has 156 modules across four packages. Every invocation compiles
the full, unmodified real `Prelude` from the pinned `aihc-base` sources and
its transitive dependency closure. Prelude is never replaced, trimmed, cached
as a semantic interface, or excluded from the timed work. Even an explicit
`--packages` selection automatically includes `aihc-base`.

`packages.tsv` records
package dependencies; `modules.tsv` records each source path, language edition
and package extensions. The executables combine these settings with each
module's own pragmas. Paths in parse trees are relative to the corpus, keeping
checksums independent of its installation directory.

## Dependency-first SCC processing

Packages are processed in dependency order: `aihc-prim`, then `aihc-base`,
then the Stackage libraries. Within a package, `ModuleGraph.dependencyUnits`
groups the module graph into strongly connected components and puts each
component after its dependencies. Edges include explicit imports, implicit
Prelude and the wired type dependencies used by the pinned compiler. The
real corpus has **115 SCC units, two cyclic**.

Each SCC is resolved with `resolveUnit`, using `collectModuleExportsWithDeps`
against the completed dependency scopes. Exports are published after the
unit succeeds. The type checker then calls
`typecheckModuleSccWithInterface` on that resolved SCC, importing dependency
interfaces and publishing its resulting facts for dependents. It retains
transitive interface facts so imported signatures and instances remain valid.
Unrelated earlier SCCs do not become implicit dependencies. Interface merges
use `TrustMergedFacts`, as a normal compiler build does; the expensive shared
fact comparisons are reserved for the compiler's lint mode.

`Wiring.hs` is a fixture copy of the pinned commit's
`bin/aihc/prim-wiring/Aihc/Prim/Wiring.hs`, with its module renamed. This is
the primitive identity and deriving-reference table required by the public
TC API. The primitive semantic interface is obtained by resolving and checking
real core sources as part of the workload.

Any parse, resolution or type error fails the process. The resolver explicitly
consumes every dynamic resolution annotation and hashes its displayed value.
The type checker consumes every binding's identity and inferred type through
its displayed `TcBindingResult`. Both force the resulting syntax trees; TC
also forces interface facts. These steps cover payloads that the parser AST's
`NFData` instance intentionally skips. Each cell checks the full tally and
checksum recorded in `corpus/frontend/workloads.json` on every sweep, then
prints `ok`. Parse, resolution, inference or checksum failures exit nonzero.
Only newly produced SCC interface facts are deeply forced: dependencies were
already forced by their own SCC, so their entire closure is not traversed again.

## Profile/backend workloads

The runner passes the corpus directory, profile and backend, using the support
merged from `origin/main`. `workloads.json` independently specifies roots,
sweep count and reference output for all 24 frontend cells. Native, LLVM and
Wasm selections differ within each profile; profiles differ within each backend.
Their root combinations include `aihc-base` alone, with `split`, with `colour`,
or with both. The real Prelude is compulsory in all combinations.

The target is approximately **0.5 seconds of AIHC process runtime**. That target
is subordinate to compiling the real Prelude. The mandatory Prelude/core work
currently exceeds it, so all cells use **one complete sweep**. The calibration
script explicitly reports `minimum_workload_above_target` when even one sweep
is too slow; it never removes Prelude or silently cuts a source/module/SCC to
meet the target. The choices provide distinct real package sets, not a claim
that every cell has reached 0.5 seconds. Timings across differing sets are not
comparable; GHC and AIHC use the same set within a cell.

## Running and recalibrating

```console
nix build .#frontend-corpus
aihc-resolve-bench ./result O2 native
aihc-tc-bench ./result O2 native
aihc-tc-bench ./result --packages aihc-base --repeat 1
python3 scripts/calibrate-frontend.py --phase tc --profile O2 --backend native \
  --corpus ./result --write -- /path/to/aihc-tc-bench
```

The calibration command must use an AIHC executable built with the selected
profile/backend. For Wasm, pass Wasmtime, its corpus directory grant, and the
precompiled artifact after `--`. It checks each tally, measures fresh processes,
adjusts sweep counts toward 0.5 seconds and optionally updates the selected
cell. Profile/backend changes do not choose a compiler flag at runtime.

Runtime includes reading sources, parsing, dependency planning, resolution,
checking where applicable, and result consumption. No network or compiler
checkout is needed at runtime. The process timeout remains 120 seconds;
profiles/backends not executed locally may still exceed it with the full Prelude.

When changing the library commit, update the corpus's core source pin and
hash as well. Regenerate freeze files and platform locks with the selected
Git packages in the development workspace, retaining the parser's exact
Hackage bound. Update the wiring fixture if its API changed, and check any
changed diagnostics, resolution targets or inferred types before replacing
`expected_stdout`. Source, corpus and dependency pins participate in the
experiment identity; earlier synthetic workload timings are not comparable.

The `graph-tests` Cabal suites cover dependency ordering, cycles, diamonds,
external imports, duplicate names and input-order independence. The Python
suite also checks corpus assembly, closure and rejected unsupported inputs.
Both self-contained copies of `ModuleGraph.hs`, `Corpus.hs` and the graph
tests must remain identical.
