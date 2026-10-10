# AIHC benchmarks

Historical runtime benchmarks for every first-parent commit on
[`ai-haskell-compiler/aihc`](https://github.com/ai-haskell-compiler/aihc)
since `aihc_since` in `benchmark.json` (2026-10-01); earlier commits are
outside the benchmark window and are never planned.

Results are published at [perf.aihc.app](https://perf.aihc.app): an overview
of AIHC against GHC for the newest measured commit, a timeline per benchmark
and metric, a page per commit, and the coverage of the commit history.

## Running locally

Requirements are Nix and Git. The GHC side resolves its dependencies against a
local Hackage package list, which `cabal update` populates. A benchmark's
freeze file pins the moment of the index it was solved against, and cabal
refuses to resolve against an index older than that pin, so a machine whose
`cabal update` predates it cannot build that benchmark at all. `doctor`
compares the two and says to run `cabal update` when the machine is behind,
and `run` refreshes the list itself, before anything is timed, whenever a
benchmark pins a newer index than the machine has. Without that a stale
index took out every GHC baseline for `aihc-cpp-stackage` and stopped a
sweep, and three weeks later the MicroHs benchmark landed with a pin newer
than every worker's list and stopped all of them -- while `doctor` called
the lists fine, because cabal writes the word `HEAD` into its timestamp
file and the check gave up on it silently. The list's own modification time
is what dates it now.

AIHC keeps a package list of its own, which `run` refreshes with the newest
compiler on the measured branch once the list is twelve hours old and then
holds still for the length of the run. The hold works by touching the
derived table, so the tarball beside it is what says how old the list is: the
table's time only said when the last run was, and one worker measured against
a three-week-old index while `doctor` reported it an hour old. The committed
`aihc.lock` of a benchmark had been solved against a newer index, so that
worker's compiler judged it stale, solved afresh and rewrote the lock in the
checkout, and since the experiment id hashes the benchmark directory, every
commit it measured from then on was filed under a suite nobody was looking
at. AIHC now compiles a scratch copy of the benchmark directory, so a
re-solve can never reach the checkout.

```console
nix develop --command cabal update
nix run . -- doctor
nix run . -- plan --fetch
nix run . -- run
```

Without `--aihc-repo` the commands use
[`ai-haskell-compiler/aihc`](https://github.com/ai-haskell-compiler/aihc),
cloned once into `.cache/aihc` and reused afterwards. Pass `--aihc-repo` (or
set `AIHC_REPOSITORY`) to benchmark another clone URL, or a local checkout
with an `origin/main` remote:

```console
nix run . -- run --aihc-repo /path/to/aihc
```

`doctor` prints the machine ID that keys this machine's results. It is derived
from the CPU model and a hashed hardware identifier and frozen in
`.state/machine.json`; pass `--machine <id>` to override it.

Run one commit with `run`, or continue until every commit has a terminal
result with `run --all`. A sweep reloads the compiler's history before each
commit it chooses -- fetching first when `--fetch` is given -- so a commit
that lands while the sweep is running is planned straight away rather than
after it finishes. A fetch that fails leaves the history as it was and says
so, since a branch briefly out of reach is no reason to stop measuring. The planner measures an unmeasured `HEAD` first, then the first commit in the
benchmark window. It then bisects the range with the largest relative change
in wall time or allocated bytes between measured endpoints. Gap width and
recency only break ties, so sharp changes are localized before quiet ranges
are filled in. When endpoints have equal values or no comparable results, it
bisects the widest gap to discover changes. Commits that leave the compiler-relevant paths
untouched inherit their neighbour's result instead of being measured.
`plan` reports measured and inherited coverage separately. Compile time is a
published metric, so compilation is as timing-sensitive as execution: both are
sequential and neither shares the machine with anything else the suite does.
Building the compiler itself is not timed and may use the whole machine.

Nothing the suite does competes with a measurement, but something else on the
machine can, and both compile time and run time move when it does -- in a way
that afterwards is indistinguishable from a change in the compiler. Since
measurement is sequential, the load average while it runs should sit near one;
`run` samples it before each cell and, when the median exceeds 2, warns and
records `contended` on the commit. The median of the samples taken after the
first two minutes, not of all of them: load average is a trailing one-minute
mean and the compile phase before it uses every core, so the early samples
measure the suite's own work rather than anyone else's. Judging them warned
on 11 of the first 12 commits on a worker that was running nothing else. A
measurement shorter than those two minutes gets no verdict at all, and
something that really shares the machine is still there once the decay has
gone. That does not make the numbers good, it makes them answerable.

Every configuration is measured in four profiles: `O0`, `O1`, `O2` and `Os`.
AIHC passes the matching `-O` flag to `aihc build`, where `-O2` and `-Os` also
compile the whole program at once. GHC has no size level, so its `Os` profile
builds with `-O1`. The four self-contained programs (factorial, fibonacci,
Snappy and SHA) take that profile name as their last argument and do a
different amount of work in each one, sized so an AIHC native run lasts
between half a second and a second. The corpus programs do not: their work
is the fixed sample, and the same tally is expected from every profile. Both
compilers apply the level to the whole build: on the GHC side that takes a
`package *` stanza in the generated project file, since cabal's command-line
`-O` and `--ghc-options` reach local packages only and would leave the
Hackage dependencies at `-O1` through the native backend.

GHC ships `text`, `bytestring`, `containers` and the rest of its boot
libraries already compiled, at the level its own release was built with, so an
`O0` profile would otherwise link optimized libraries where AIHC compiled its
equivalents at `-O0`. The GHC side rebuilds them from source instead, at the
profile's level. Their versions are left to the solver rather than pinned to
what the compiler ships, so the bounds a benchmark and its dependencies
declare still decide: GHC 9.14.1 ships `time-1.15` while `snappy-hs` requires
`time <1.15`, and pinning the shipped version made that benchmark unsolvable.
The freeze file's `index-state` is what keeps the choice deterministic.
`base`, `ghc-prim`, `ghc-internal`, `ghc-bignum`, `rts`, `template-haskell`
and what they depend on are wired into the compiler and stay as shipped, so a
benchmark that only uses `base` is unaffected. A rebuilt boot library may
predate the `base` it is rebuilt against (`array-0.5.8.0` caps `base <4.22`),
so its bounds on those wired-in packages are relaxed -- and only those. Its
bounds on other boot libraries still hold: relaxing every bound let the
solver keep `unix`'s `os-string` flag off, the branch that wants
`filepath <1.5`, while choosing `filepath-1.5`, and `unix` failed to compile.

Both compilers are timed installing every dependency a benchmark has,
`text` included. The only libraries prepared beforehand are the compiler's
own: the packages GHC wires in, and AIHC's core libraries -- `aihc-base`,
`aihc-internal`, `aihc-prim`, `aihc-rts` and `aihc-template-haskell`, the set
AIHC's own lock marks `"source": "core"`. Preparing `aihc-base` alone left the
rest inside the timed compile, so a benchmark reaching `bytestring` rebuilt
about 11 MB of core library in every cell while GHC never rebuilds its
wired-in closure at all. Every artifact is stripped before its size is recorded: `llvm-strip` for native binaries and
`wasm-tools strip --all` for Wasm, which also handles the component AIHC
emits. Stripping happens after the timed compile. GHC is measured with its
native and LLVM backends, and with the `ghc-wasm-meta` cross-compiler for
Wasm. GHC targets `wasm32-wasi` while AIHC targets `wasm32-wasip3`; both run
under Wasmtime, so the Wasm ratio includes the difference between the two host
interfaces' startup costs. Wasmtime's own code generation is not part of it:
after the timed compile and the strip, every Wasm artifact is precompiled
with `wasmtime compile` and the runtime measurements execute the machine code
with `--allow-precompiled`. Before that, a Wasm invocation was mostly
Cranelift compiling the module. The precompile step is timed by nobody, and
`artifact_size` remains the stripped Wasm. Every GHC configuration, Wasm included, builds the
benchmark's Cabal package with `cabal build` against the flake's toolchain
(`ghc-<version>` plus its `ghc-pkg`/`hsc2hs` siblings) and a private store per
configuration, so Hackage dependencies are resolved and compiled inside the
timed step for every configuration rather than only the first; a benchmark's
`cabal.project.freeze` pins those dependencies while boot libraries come from
whichever GHC is under test. The freeze file's flag assignments reach every
package that is built, a rebuilt boot library included; `cabal freeze` writes
none for a boot library, so a benchmark that needs one adds the line by hand
(`microhs-self-compile` turns off `haskeline`'s `terminfo`).

AIHC builds the same package directory with `aihc build`, which reads the
`.cabal` file and resolves its dependencies itself rather than the freeze
file. Non-boot Hackage dependencies therefore carry an exact `==` bound in the
benchmark's `.cabal` (matching the freeze pin), so both toolchains compile the
same version of, say, `snappy-hs`. Boot libraries are deliberately left
unbounded: GHC ships its own and the versions differ per release, so pinning
them in the `.cabal` would break every toolchain but the one the freeze file
was written under.

## Benchmarking with `./bench`

```console
./bench
./bench --once
./bench window add 22:00-06:00
```

`./bench` measures one commit after another and says what it is doing. Before
each commit it prints which commit is next and why: `HEAD` because it is the
newest commit and has no result, `TAIL` because it is the oldest commit in the
benchmark window, or the size of the wall time or allocation difference
between its measured neighbours, and on which benchmark and configuration that
difference was seen. It estimates how long the commit will take -- the median
wall clock of the last ten commits this machine uploaded, or, on a checkout
with no local history, of the last commit the site has from this machine --
and how long the rest of the history will take, counting one commit per
compiler tree since the others inherit its result. `--once` prints that and
exits. Every other command passes through, so `./bench plan` and
`./bench doctor` work as before.

A benchmark starts only when the machine may measure:

- inside a time window, when any are set. `./bench window add 22:00-06:00`
  (also `22-6` or `10pm-6am`) adds one in local time, `window remove` and
  `window clear` take them away, and `window` lists them. They live in
  `.state/schedule.json` and are re-read every second, so a change applies to
  a running `./bench` at once. Outside the windows it counts down to the next
  start, and the estimate of when the history is done accounts for them. A
  commit started inside a window is allowed to finish past its end.
- on mains power. A laptop on battery lowers its clock speeds, which moves
  every timing exactly as a change in the compiler would. macOS is asked
  through `pmset`, Linux through `/sys/class/power_supply`; a machine without
  a battery is on mains. `--allow-battery` overrides it.
- with no other run on the machine. `run` holds `.state/run.lock` while it
  measures, and a second `run` exits 3 rather than measuring the first.

Each commit is measured by its own `run --fetch --upload` process
(`--no-upload` keeps results local), whose output streams beneath a status
line with the elapsed and expected time. A run that fails is retried after a
minute, doubling to at most half an hour while it keeps failing.

`./bench` follows both repositories. Every five minutes (`--poll`) it fetches
the compiler and replans, so a commit that lands is considered at once. It
fetches this checkout's upstream too and fast-forwards to it between commits,
never into local commits, and when the checkout changes -- that pull, a local
commit, or an edit to a tracked file -- it restarts itself through `nix run`
so the change is what measures the next commit. A commit being measured is
never interrupted: the change applies once it finishes. `nix run` sees only
tracked files, so a new file takes a `git add` before it is part of a restart.

## The Wasm sysroot

Since [ai-haskell-compiler/aihc@e8f97b72](https://github.com/ai-haskell-compiler/aihc/commit/e8f97b72e076ac9afcba9323c0001d1d854ed100)
(2026-10-05) the compiler links the `wasm32-wasip3` libc that wasi-sdk 34
builds and refuses any other sysroot; the wasi-libc of nixpkgs, which the
flake assembles into `AIHC_WASM_SYSROOT`, is built for preview 1 and calls
the host through imports a component cannot have. Every AIHC Wasm cell on
every machine failed to compile from that commit on. The runner fetches the
two wasi-sdk release assets once, pinned by hash as the compiler's own
`scripts/nix/wasi-sysroot.nix` pins them, cuts them down to the one target
under `.cache/wasi-sysroot-34.0`, and points `AIHC_WASM_SYSROOT` there for
every commit that contains e8f97b72. Earlier commits keep the flake's. The
same commits also link with `wasm-component-ld`, which the flake does not
carry; the runner builds it from the nixpkgs the flake locks (the root
flake's, not `ghc-wasm-meta`'s) and roots it under `.cache/gcroots`, and
puts it on `PATH` for the commit. Both are provided by the runner rather
than added to `flake.nix` because the experiment ids hash that file, and a
change to it restarts every history. A fetch or build that fails is
reported and the Wasm cells fail with the compiler's own message, as
before; the commit is still measured on the other backends. The two come
together or not at all: a sysroot without the linker only moves the failure
from preparing the store to the end of every cell's compile. The components
that libc produces import `wasi:http`, so for the same commits the runner
adds `-S http` to the AIHC Wasm run commands beside the `-S cli` that
`benchmark.json` carries -- there rather than in the configuration, which
the experiment ids also hash.

## The CPP corpus

`aihc-cpp-stackage` measures [`aihc-cpp`](https://github.com/ai-haskell-compiler/aihc-cpp),
the pure Haskell C preprocessor the compiler uses, on every CPP-using module of
a pinned Stackage snapshot. The program is an ordinary benchmark package that
both toolchains compile; its input is a corpus the flake builds:

```console
nix build .#cpp-corpus
cat result/report.txt
```

The corpus is a plain directory: one subdirectory per package holding the
modules that contain preprocessor directives, the `.cabal` file and every file
they `#include`, plus what a real build would add and a bare source tree
lacks. `generated/<package>/cabal_macros.h` carries `MIN_VERSION_` and
`VERSION_` for the package's dependencies at the snapshot's versions, the way
Cabal writes it; `generated/ghcversion.h` and `macros.tsv` carry the
`__GLASGOW_HASKELL__` family for the snapshot's compiler and a fixed
`x86_64`/`linux` platform, so every machine preprocesses the same branches;
`include/` holds stand-ins for headers GHC ships, such as `MachDeps.h`.
`modules.tsv` lists each module with the headers to pre-include and the
directories to search, and `packages.tsv`, `report.txt` and
`unresolved-includes.txt` say what went in and which includes nothing
satisfies (a package-local header only its `configure` script generates, or
one behind a platform conditional the corpus never takes). Because every
decision is written out, a wrong macro or a missing header can be found by
reading the corpus rather than the program.

The timed run sweeps `benchmark.tsv`, an even stride through the corpus up
to 512 KiB of module source (`sampleBytes` in `corpus/cpp/corpus.nix`; zero
selects everything). A GHC build does the whole 75 MB in about two seconds,
but an AIHC build preprocesses around 40 KB/s today, so the sample is what
keeps an AIHC run inside the benchmark's own `process_timeout_seconds`.
`<program> <corpus> --report` sweeps every module and prints each
diagnostic, which is how the stand-in headers and macros were chosen: what
remains is Windows-only branches, headers only a package's `configure`
script writes, and includes of test files that live outside the package.

The snapshot is pinned in `corpus/stackage/lts-24.58.json` with the SHA-256
of every package tarball, so each is a fixed-output fetch and a failure names
the package. The pin also records the versions of the packages the snapshot's compiler
ships, which Stackage lists as `core`, so nothing is compiled or installed to
build the corpus. Bump it with `corpus/stackage/update.py`, which needs only
the `all-cabal-hashes` tarball from nixpkgs; the docstring has the commands. The runner receives the
built corpus through `AIHC_BENCH_CPP_CORPUS`, exported by the flake like the
toolchains, and passes it to the program as its argument (preopened with
`--dir` under Wasmtime). The printed tally -- module count, modules without an
error diagnostic, total output bytes -- is the benchmark's expected output,
so a miscompiled preprocessor fails the run rather than producing a number.
A corpus benchmark names the directories its corpus is built from in
`corpus_sources` (`corpus/cpp` and the shared snapshot pin under
`corpus/stackage` for this one), and those files are part of that benchmark's
experiment identity and of no other: a change to one corpus restarts the
history of the benchmark that reads it and leaves the others valid.

## The parser corpus

`aihc-parser-stackage` measures [`aihc-parser`](https://github.com/ai-haskell-compiler/aihc-parser),
the compiler's Haskell parser, on the modules of the same snapshot that a
parser can read as they sit on disk. Its corpus is built the same way:

```console
nix build .#parser-corpus
cat result/report.txt
```

A module is selected when the package's `.cabal` file declares it (so test
fixtures meant not to parse stay out), it is plain `.hs` rather than `.lhs`
or `.hsc`, it is valid UTF-8 without a byte-order mark, and no preprocessor
would touch it: neither CPP, whether a `LANGUAGE` pragma or the package's
`default-extensions` enable it, nor a custom one named with `-pgmF`. A
package that enables CPP anywhere is left out whole, since a module of it may
rely on the preprocessor without saying so. That keeps about 35,000 of the
snapshot's modules, 240 MB of source. `modules.tsv` lists each with the
package's `default-language` and `default-extensions`, which is what a build
hands the parser before the module's own `LANGUAGE` pragmas; the `.cabal`
file is read loosely, every stanza contributing, so the extension set is a
superset of any one component's.

The timed run sweeps `benchmark.tsv`, an even stride through the corpus up
to 32 KiB of module source (`sampleBytes` in `corpus/parser/corpus.nix`)
that steps over modules above 16 KiB, so it is many hand-written modules
rather than one generated one. An AIHC build at `-O0` parses hand-written
modules at around 17 KB/s natively today, so a native run takes about two
seconds; under Wasmtime it parses around 4 KB/s after two seconds of
startup, about eleven seconds in all, and the sample is what keeps that
inside the benchmark's `process_timeout_seconds`. A GHC build at `-O0`
reads the whole corpus in about two minutes. Every module is read, decoded, parsed and forced completely --
the whole tree and the recovered parse errors -- before the next one
starts, as a compiler front end would consume it. `<program> <corpus>
--report` sweeps every module and prints each parse error. The printed
tally -- module count, modules without a parse error, imports and
declarations across every tree -- is the benchmark's expected output, so a
miscompiled parser fails the run rather than producing a number.

Before the timed compile the runner installs `aihc-base` and the dependencies
GHC ships as boot libraries into a per-commit store, once per target and
optimization level, so both toolchains pay for the same work inside the timed
step: the benchmark and its non-boot Hackage dependencies.

Every commit in a run resolves against one Hackage index. AIHC refetches
its index once the derived table is a day old, and a sweep runs for days, so
the index would otherwise be replaced partway through -- the commits after
the refresh resolving against a different Hackage than the ones before, in
one continuous series. `run` holds the cached index still for the length of
the run, and records its digest in each result's environment, so two results
that resolved differently can be told apart. Picking up a newer index is a
decision between runs rather than one discovered mid-series.

Before measuring anything, `run` refreshes AIHC's Hackage index with the
compiler at `aihc_ref`. A measured commit that found the index stale would
refresh it with its own code, so whether a historical commit builds would
depend on how old the cache happened to be when it was scheduled -- and a
failure is terminal. Warming it first means every commit in a run resolves
against the same index and none of them performs the refresh. A warming
failure is reported and the run continues against whatever is cached.

A fault of the machine is never published as a result. The run stops with
exit code 2 and records nothing for the commit, so the attempt stays unfinished
and the commit is measured again once the machine is sound; no `forget` is
needed. The faults are:

- a toolchain that fails verification before measuring (`nix store verify`,
  and on macOS `codesign` on every executable and library) or that Nix cannot
  build or root for reasons of the machine (network, disk, a killed builder);
- a compiler, or its store preparation, killed with SIGKILL -- nothing a
  compiler does ends that way, while macOS rejecting a code signature and an
  out-of-memory killer both do;
- a program whose code signature stripping broke (macOS);
- a benchmark whose baseline compiler produced nothing. Every AIHC number is
  published as a ratio against GHC, so an AIHC-only result is not a partial
  result but a useless one.

A compiler that fails to build, crashes, or links a program macOS will not run
is the commit's result and is recorded as such.

Results and resumable state are stored in `.state/benchmarks.sqlite3`. A failed
historical compiler is terminal until its record is deliberately removed:

```console
nix run . -- forget <commit>
```

## MicroHs compiling itself

`microhs-self-compile` builds [MicroHs](https://github.com/augustss/MicroHs),
a Haskell compiler written in Haskell, and runs it on its own sources: what
MicroHs's `Makefile` does to regenerate `generated/mhs.c`. Every module of
the compiler and of its own Prelude and base library is parsed,
type-checked, desugared and translated to combinators, and the combinators
are written out as one C array. The compiler is the `MicroHs-0.16.0.0`
release, its `ghc/` and `src/` directories vendored verbatim under
`benchmarks/microhs-self-compile/microhs` (Apache-2.0); the input is the same
release's `lib`, `mhs` and `src`, built by the flake from the Hackage tarball:

```console
nix build .#microhs-corpus
```

`Main.hs` runs MicroHs's own `main` from inside the corpus, naming the
sources relatively as the `Makefile` does, since MicroHs records source
locations in what it generates. It prints the size and the FNV-1a hash of the
generated C, which is the benchmark's expected output; every native and LLVM
build prints the same line. A GHC `-O1` build takes about 1.7 seconds, and
AIHC `-O0` builds take about 24 natively and 55 through LLVM, so the
benchmark has its own `process_timeout_seconds`.

MicroHs depends on `haskeline` for its REPL, which the benchmark never
starts, and `haskeline` reaches for `terminfo`, which needs a curses library
the toolchain environment does not have. Both toolchains build `haskeline`
without it: the freeze file carries `haskeline -terminfo` for GHC, and
`aihc.lock` records the same flag for AIHC.

The benchmark is not measured on Wasm: `unsupported_backends` lists `wasm`,
and those cells are recorded unavailable (`unsupported_backend`) without
being compiled. AIHC's `wasm32-wasip3` runtime reaches the host through WASI
preview 3 only and refuses a program that imports preview 1, which the C code
of `unix`, `directory`, `file-io` and `time` needs. GHC's Wasm build does
run, but its `Int` is 32 bits, so MicroHs writes six of its literals wrapped
and the output no longer matches the native one.

To move to another release, change `version` and the tarball hash in
`corpus/microhs/corpus.nix`, replace the vendored directories with the
release's `ghc/`, `src/MicroHs`, `src/Text` and `src/runtime/MachDeps.h`,
update the module list in the `.cabal` file from the release's, and
regenerate the freeze file, `aihc.lock` and the expected output.

## Comparing two builds

```console
nix run . -- compare HEAD~5 HEAD
nix run . -- compare main --worktree /path/to/aihc \
  --bench integer-fibonacci-v1 --rounds 20
```

`compare` builds both compilers, compiles the selected benchmarks with each, and
runs them in interleaved A, B, A, B rounds, after one discarded warm-up round,
so machine drift affects both sides equally. It prints medians, the change of B
relative to A, and a bootstrap 95% interval on that change; `--markdown` formats
the table for a pull request. Results are stored locally in `adhoc_runs` and
never uploaded.

## Measurement

The runner measures complete process invocations, including native startup and
Wasmtime startup, but not Wasmtime's compilation of the module, which happens
once before measuring (see above). It records wall time, CPU time and peak RSS for run buckets
of 1, 2, 4, 8, 16, 32, and 64 processes, stopping when adjacent bucket means
are within 1%, when the buckets run out, or when the cell has spent
`cell_budget_seconds`. Doubling a bucket costs whatever an invocation costs,
and that spans four orders of magnitude here: the integer benchmarks run in
about ten milliseconds, so the full escalation costs a second and buys
precision cheaply, while `aihc-cpp-stackage` takes about ten seconds an
invocation, where the same escalation is twenty-one minutes for a single
cell. The budget bounds the expensive cells without taking precision from
the cheap ones, which a smaller bucket limit would. Peak heap, bytes allocated, GC count, GC time and the longest GC pause come from GHC's
`+RTS -t` output and from the `AIHC_RTS_STATS` hook once the AIHC runtime has
it; the longest pause is optional in the hook, so a runtime that reports the
rest but not it leaves only that metric unavailable.

Every invocation also records the instructions it retired and the cycles it
took, from the CPU's own counters. Wall time moves with whatever else the
machine is doing; the instruction count barely moves at all -- repeated runs
agree to a few parts in a million on Linux -- so a change in the compiler
shows up in it even where timing noise hides it. On Linux the runner opens
`perf_event_open` counters on itself that every child inherits and enables
when it calls `exec`, so the count covers the benchmark process and anything
it starts, in user space only; that needs `perf_event_paranoid` at 2 or
lower, the kernel default. On macOS it reads `ri_instructions` and
`ri_cycles` from `proc_pid_rusage` once the process has exited and before
it is reaped, which covers that one process in user and kernel mode. The
two are therefore not comparable across platforms, which machine ids keep
apart anyway. A counter that was multiplexed with others only ran part of
the time and is recorded unavailable rather than extrapolated, and `doctor`
reports whether this machine can count at all. A GHC baseline measured
before a metric existed is not reused, since it would leave that metric's
ratio empty for the whole reuse window. Compile time and artifact size are recorded per compilation. Every
configuration is compiled from scratch -- the build directory and any previous
artifact are removed first -- so compile time always describes a full compile
rather than an incremental no-op. Both compilers run with `+RTS -N -RTS` and so
use every core, which makes compile time a multithreaded measurement of one
compiler with the machine to itself.

A GHC configuration is not measured again for every AIHC commit. Its inputs
are the benchmark, the toolchain and the machine, and none of them is the
commit under test, so the number cannot have moved. A GHC result is reused
for `baseline_reuse_hours` (720, thirty days, by default; zero measures everything every
time), after which it is measured again, and a changed `environment_id` -- a
new OS, CPU or runner -- discards it at once. What this trades away is drift
cancelling: an AIHC number measured now against a baseline measured earlier
carries whatever the machine did in between, which the window bounds. Reused
entries record `reused_from`, naming the commit and moment their baseline
came from. The AIHC side is never reused: its compiler is the commit.

Each commit records its wall clock per phase -- building the compiler,
preparing the AIHC stores, compiling, measuring -- and `run` prints the
breakdown and warns when a commit exceeds `commit_budget_seconds` (an hour by
default; zero disables the check). Building the compiler and preparing its
stores happen once per commit and need no benchmark, so they can dominate an
hour without appearing in any per-cell number; the breakdown is what makes a
slow commit explainable rather than merely slow.

All raw samples and the stopping reason
are retained.

## The worker machines

`deploy/` holds what the two measuring machines run, so that the code behind
every published number is in the repository rather than only in somebody's
`~/bin`. Install it on a machine with

```
deploy/install.sh continuous|window [repository]
```

which writes `~/bin/aihc-bench.sh`, a user unit, and a drop-in giving it a
PATH with nix on it -- a systemd user service does not source login profiles,
and after a reboot the window died on `nix: command not found`. There is one
unit whatever the mode: two runners on one machine measure each other.

`continuous` measures one commit after another until every commit has a
terminal result, then waits for new ones; it suits a machine that does nothing
else. `window` measures one
commit at a time and only starts one between 22:00 and 06:00, for a machine
that hosts CI runners the rest of the day. A commit started just before 06:00
is allowed to finish past it.

Both fast-forward the checkout to `origin/main` between commits. A worker
running an old checkout publishes a different suite from its sibling and
nothing in the results says so, which is how one machine spent a day four
commits behind. Neither runs `run --all`, which returns only once the whole
history is measured and so kept one worker on a days-old checkout.

`~/bin/aihc-bench.sh` is a copy, not a link, so a change under `deploy/`
reaches a machine only when `deploy/install.sh` is run there again.

## Uploading

Results are served by a Cloudflare Worker at
[perf.aihc.app](https://perf.aihc.app), whose source and static pages live in
`web/`. Uploading needs nothing beyond a Cloudflare login on the machine:

```console
wrangler login
nix run . -- upload
nix run . -- run --all --upload
```

The uploader writes envelopes to the R2 bucket and index rows to the D1
database through `wrangler`, so whoever can log in to the Cloudflare account
can upload, and the Worker itself is read-only. A wrangler call that fails
with an expired OAuth token (`Authentication error [code: 10000]`, which
`wrangler whoami` does not see) or a network fault is retried with a growing
backoff; anything else fails at once. Uploads are idempotent, so a retry
cannot double-write. `upload` pushes the commit
list and every run not yet acknowledged; `run --upload` does the same after
each commit. Uploads are idempotent. Machines appear on the site under their
derived id, such as `apple-m4-pro-542f1e`.

## Deploying the Worker

```console
cd web && npm ci && npm run check && npm test
npm run migrate && npm run deploy
```

Pushes to `main` that touch `web/` deploy through GitHub Actions using the
`CLOUDFLARE_API_TOKEN` repository secret, which needs the Workers Scripts and
D1 edit permissions.

See [docs/architecture.md](docs/architecture.md) for the data contract and
[docs/design.md](docs/design.md) for the Worker API.
