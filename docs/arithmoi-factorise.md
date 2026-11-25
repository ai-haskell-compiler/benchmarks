# arithmoi factorisation

`arithmoi-factorise-v1` factorises fixed integers and searches for the prime after fixed starting points.
It uses the Hackage library [`arithmoi`](https://hackage.haskell.org/package/arithmoi) version 0.13.4.0.
The benchmark is the first in the suite that exercises a large dependency tree: 52 packages, among them `vector`, `containers`, `bitvec`, `chimera`, `mod` and `semirings`.

## Workload

Each backend and profile has its own fixed case list, and the program runs that list exactly once.
The cells differ in the size of the lists, not in repetition: a repeated batch is work a sufficiently clever compiler could share, even behind an `IORef`.
A full list has these kinds of case:

- Balanced semiprimes with two prime factors of equal size.
  Trial division finds nothing, so arithmoi runs the elliptic curve method over `Mod n` with Montgomery curves.
  This is the costly case, and the number and size of these set the cell's run time.
- Unbalanced semiprimes with a 16-bit and a 40-bit prime factor.
  Trial division finds the small factor; the cofactor is proved prime with Baillie-PSW.
- Products of three 19-bit primes.
- Prime powers with exponents from 2 to 17.
- Smooth numbers with repeated small primes.
- Primes of 62 bits, which `factorise` must recognise as prime.
- Six edge cases: 1, 2, 2^62, 3^39, 2^31 - 1 and its square.
- Prime searches with `nextPrime` from fixed starting points.

Lists without elliptic curve cases leave out the balanced semiprimes and the products of three 19-bit primes, the only kinds that reach the elliptic curve method (see below), and scale the other kinds instead.
Sizes as of 2026-11-25 (AIHC `3b5102082`, Apple M4 Pro):

| Cell | Elliptic curve cases | Balanced semiprimes (30-bit factors) | Cases of each other kind | Prime searches | Search bits |
| --- | --- | ---: | ---: | ---: | ---: |
| native O0 | no | 0 | 167 | 83 | 128 |
| native Os | yes | 365 | 8 | 4 | 192 |
| native O1 | yes | 556 | 8 | 4 | 192 |
| native O2 | yes | 766 | 8 | 4 | 192 |
| LLVM O0 | no | 0 | 36 | 18 | 128 |
| LLVM Os | yes | 308 | 8 | 4 | 192 |
| LLVM O1 | yes | 507 | 8 | 4 | 192 |
| LLVM O2 | yes | 718 | 8 | 4 | 192 |
| Wasm O0 | no | 0 | 28 | 14 | 128 |
| Wasm Os | no | 0 | 113 | 56 | 128 |
| Wasm O1 | yes | 196 | 8 | 4 | 192 |
| Wasm O2 | yes | 279 | 8 | 4 | 192 |

Each factorisation result is sorted by prime and compared with the complete reference factorisation.
Each prime search result is compared with the reference prime.
Any difference produces `fail` and a nonzero exit status.

## Fixed reference inputs

`scripts/integer-fixtures.py` creates the fixture module from a fixed seed, next to the RSA and convex hull fixtures.
`ARITHMOI_LISTS` and `ARITHMOI_CELLS` in that script hold the table above.
Every factorisation is known by construction: the script chooses primes with a Miller-Rabin test over the first 64 prime bases, multiplies them and records the exponents.
No factoring algorithm takes part in the reference.
The reference prime of each search is the first Miller-Rabin probable prime after the starting point.

To recreate or check the fixtures, execute `python3 scripts/integer-fixtures.py` or `python3 scripts/integer-fixtures.py --check` from the repository root.

## Sieve startup

The elliptic curve method takes its stage-one primes from arithmoi's prime sieve, a lazy list of 128 KiB sieve blocks.
The first block is sieved once per process, on the first composite that survives trial division; only balanced semiprimes and products of three 19-bit primes get that far.
On 2026-10-12 that block cost about 1.3 seconds at `O0` and `O1` with native AIHC on an Apple M4 Pro, so every list below `O2` was the smallest one that still reached the elliptic curve method, and the sieve was most of its run time.
By 2026-11-25 (AIHC `3b5102082`) it cost about 0.03 seconds at `O1` and `O2` and 0.2 seconds at native and LLVM `Os`, but still about 0.9 seconds at native `O0`, 3.3 at LLVM `O0`, 4.5 at Wasm `O0` and 1.1 at Wasm `Os` (CPU time).
Those four cells therefore use lists without elliptic curve cases, which measure trial division, Baillie-PSW and `nextPrime`; the others are sized with balanced semiprimes.
GHC sieves the block in under 0.2 seconds at `O0`.

## Runtime target (2026-10-12)

These figures predate the per-cell lists above. The reference compiler commit is `1ef9d84b9`.
The measurement date is 2026-10-12.
The table gives median elapsed times for three runs after one initial run with native AIHC on an Apple M4 Pro.
GHC's `Os` column is its `O1` build.

| Profile | AIHC seconds | GHC seconds |
| --- | ---: | ---: |
| O0 | 1.37 | 0.18 |
| Os | 0.65 | 0.03 |
| O1 | 1.31 | 0.03 |
| O2 | 0.52 | 0.09 |

The AIHC Wasm build at `O2` runs its list in 0.93 seconds under Wasmtime on the same machine.
