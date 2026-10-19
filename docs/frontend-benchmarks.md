# Name resolution and type inference

`aihc-resolve-bench-v1` and `aihc-tc-bench-v1` exercise the public APIs of
AIHC's name resolver and type checker. Both use the `git_dependencies`
support in `benchmark.json`, pinned to AIHC commit
`6ceb5ca6b5e6308fb48ee61a557aa6274aa54c2d` (2026-10-19). The resolver
benchmark selects `components/aihc-resolve`; the type checker selects that
package and `components/aihc-tc`. The commit is independent of the compiler
revision being measured.

Both benchmarks depend on **Hackage `aihc-parser-5.0.0.0`**. No parser source
is selected from Git. Their `.cabal` files pin its version, Cabal freeze
files pin the Hackage solution, and `aihc.lock` records the parser as
`hackage` and the selected Git libraries as `local` on macOS ARM64, Linux
x86-64 and WASI wasm32. LLVM uses the host platform's plan.

The runner fetches the pinned repository before starting the compile clock,
then gives each configuration fresh sources through Cabal local packages or
AIHC's workspace. Compilation of the libraries remains timed. Runtime
inputs are generated deterministically in the executables; neither workload
requires a corpus, a compiler checkout, or network access at runtime.

## Resolver workload

The workload generates 24 modules, each with 20 groups of functions and a
recursive tree type. Each module after the first imports its predecessor
under a qualified alias. Local helpers shadow outer binders, recursive
patterns introduce new bindings, and right-associative operators exercise
fixity handling. The benchmark builds the module export table and resolves
the whole unit.

It walks every result's annotations and computes a 64-bit FNV-1a checksum of
the displayed resolutions, including source spans, identifiers, namespaces
and target entities. This explicitly consumes the dynamic annotations that
the parser AST's `NFData` instance skips. It also forces the resulting trees.
The expected output is:

```text
modules=24 resolutions=28700 fnv1a=6305761367544039928
```

## Type checker workload

The workload generates 16 modules, each with 10 groups of functions. It
checks polymorphic identities and composition, explicit signatures,
recursive tree traversals, optional-value binds, local generalization, a
higher-kinded mapping class and two instances. Modules have no Prelude
dependency: their data types and classes are declared in the input itself.

The public API requires the embedding compiler to provide its primitive
wiring and a semantic interface for primitive kinds. `Wiring.hs` is a
fixture copy of the pinned commit's
`bin/aihc/prim-wiring/Aihc/Prim/Wiring.hs`, with its module renamed. A tiny
`GHC.Types` input supplies `TYPE`, `RuntimeRep`, `BoxedRep`, `Levity` and
`Lifted`; it is resolved and checked once before the generated modules.
These fixtures keep runtime inputs independent of the compiler under test.

After parsing and resolution, `typecheckModulesWithInterface` checks the
modules. Any parse, resolution or type error fails the process. The benchmark
consumes every binding's identity and inferred type through a checksum of
its displayed `TcBindingResult`, then forces the checked syntax trees. The
expected output is:

```text
modules=16 bindings=1552 fnv1a=2128632125511863541
```

## Running and updating

Both executables take no arguments for the full workload. `--small` checks
two modules with two groups each, for quick development checks. The full
workload is the same for every optimization profile. The suite records
the exact output above and allows 120 seconds per invocation.

Runtime measurements include source generation, parsing, resolution,
checking where applicable, and result consumption. They measure a small
frontend pipeline rather than isolated API call timings. Compilation also
includes the pinned libraries and their dependencies.

When updating the library commit, stage the selected packages with the Git
dependency helper, regenerate `cabal.project.freeze` with those local
packages in the development project, and regenerate each platform's
`aihc.lock` with the same workspace. Keep the parser's exact Hackage bound.
Update the wiring fixture if its API changed, run both workloads with a
baseline compiler, and replace `expected_stdout` only after checking any
changed diagnostics, resolution targets or inferred types. Pin changes are
part of the benchmark experiment identity, so their results form a new
series automatically.
