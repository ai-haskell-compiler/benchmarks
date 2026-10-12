# arithmoi factorisation

`arithmoi-factorise-v1` factorises fixed integers and searches for the prime after fixed starting points.
It uses the Hackage library [`arithmoi`](https://hackage.haskell.org/package/arithmoi) version 0.13.4.0.
The benchmark is the first in the suite that exercises a large dependency tree: 52 packages, among them `vector`, `containers`, `bitvec`, `chimera`, `mod` and `semirings`.

## Workload

Each profile has its own fixed case list, and the program runs that list exactly once.
The profiles differ in the size of the numbers, not in repetition: a repeated batch is work a sufficiently clever compiler could share, even behind an `IORef`.
Every list has the same shape:

- Balanced semiprimes with two prime factors of equal size.
  Trial division finds nothing, so arithmoi runs the elliptic curve method over `Mod n` with Montgomery curves.
  This is the costly case, and the number and size of these set the profile's run time.
- Unbalanced semiprimes with a 16-bit and a 40-bit prime factor.
  Trial division finds the small factor; the cofactor is proved prime with Baillie-PSW.
- Products of three 19-bit primes.
- Prime powers with exponents from 2 to 17.
- Smooth numbers with repeated small primes.
- Primes of 62 bits, which `factorise` must recognise as prime.
- Six edge cases: 1, 2, 2^62, 3^39, 2^31 - 1 and its square.
- Prime searches with `nextPrime` from fixed starting points.

| Profile | Balanced semiprimes | Bits per factor | Cases of each other kind | Prime searches | Search bits |
| --- | ---: | ---: | ---: | ---: | ---: |
| O0 | 4 | 26 | 4 | 2 | 128 |
| Os | 4 | 26 | 4 | 2 | 128 |
| O1 | 4 | 26 | 4 | 2 | 128 |
| O2 | 28 | 30 | 8 | 4 | 192 |

Each factorisation result is sorted by prime and compared with the complete reference factorisation.
Each prime search result is compared with the reference prime.
Any difference produces `fail` and a nonzero exit status.

## Fixed reference inputs

`scripts/integer-fixtures.py` creates the fixture module from a fixed seed, next to the RSA and convex hull fixtures.
`ARITHMOI_PROFILES` in that script holds the table above.
Every factorisation is known by construction: the script chooses primes with a Miller-Rabin test over the first 64 prime bases, multiplies them and records the exponents.
No factoring algorithm takes part in the reference.
The reference prime of each search is the first Miller-Rabin probable prime after the starting point.

To recreate or check the fixtures, execute `python3 scripts/integer-fixtures.py` or `python3 scripts/integer-fixtures.py --check` from the repository root.

## Sieve startup below O2

The elliptic curve method takes its stage-one primes from arithmoi's prime sieve, a lazy list of 128 KiB sieve blocks.
The first block is sieved once per process, on the first composite that survives trial division.
With native AIHC on an Apple M4 Pro that block costs about 1.3 seconds at `O0` and `O1` and 0.6 seconds at `Os`, against 0.02 seconds at `O2`.
GHC sieves it in under 0.2 seconds at `O0`.
The cases themselves cost 0.05 to 0.2 seconds at those levels.

The lists below `O2` are therefore the smallest that still reach the elliptic curve method, and the sieve is most of their run time.
Shrinking the numbers further would skip the elliptic curve method and measure trial division alone, which is a different program.
The `O2` list is sized for the usual half-second target.

## Runtime target

The reference compiler commit is `1ef9d84b9`.
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
