# Integer workloads

The suite replaces `integer-factorial-v1` and `integer-fibonacci-v1` with two application benchmarks.
The total benchmark count stays at six.
Each replacement has a new benchmark ID.

## RSA signature verification

`rsa-verify-v1` verifies PKCS #1 v1.5 signatures for fixed SHA-256 digests.
It uses 2048-bit and 4096-bit public keys with exponent 65537.
The digest inputs exclude the cost of SHA-256 computation.
The SHA benchmark measures that cost separately.

Each key has six cases:

- A valid signature.
- A corrupt signature.
- An incorrect digest.
- A signature with incorrect padding.
- A negative signature representative.
- A signature representative equal to the modulus.

Each case has a complete reference result and an expected verdict.
The benchmark compares the complete recovered Integer with the reference result.
It then compares the complete encoded digest block with that Integer.
It rejects signature representatives outside the permitted range.
The padding format follows [RFC 8017, section 9.2](https://www.rfc-editor.org/rfc/rfc8017#section-9.2).

Both compilers execute the same Haskell modular exponentiation algorithm.
Each multiplication has a modular reduction.
Strict accumulators prevent a long chain of arithmetic thunks.

## Exact convex hull computation

`exact-convex-hull-v1` computes a two-dimensional convex hull with Andrew's monotone chain algorithm.
It uses Integer coordinates and exact orientation determinants.
The output starts at the least point and proceeds counterclockwise.
It excludes duplicate vertices and interior points on straight edges.

The main input has 1,011 fixed points on an integer coordinate grid.
The coordinate magnitudes do not exceed one trillion.
Their products exceed the range of a 64-bit signed integer.
The input contains a point cloud, points near a straight line, and duplicate points.
Additional inputs cover empty sets, single points, duplicate points, straight lines, and determinants whose exact value is one.
The benchmark compares every output vertex with a fixed reference hull.

## Fixed reference inputs

`scripts/integer-fixtures.py` creates both fixture modules from fixed seeds.
Python modular exponentiation supplies each RSA reference result.
Python gift wrap supplies each reference hull independently of the benchmark algorithm.
The runner tests check the fixture files against these reference computations.
The benchmark programs do not create random inputs or read external files.

To recreate the fixtures, execute this command from the benchmarks repository root:

```sh
python3 scripts/integer-fixtures.py
```

To check the fixtures, execute this command:

```sh
python3 scripts/integer-fixtures.py --check
```

## Runtime target

The profile counts target 0.5 seconds with native AIHC on an Apple M4 Pro.
The reference compiler commit is `db57916ba`.
The measurement date is 2026-10-11.
Each profile uses the same fixed input cases.
Only the number of complete input batches changes.
Both compilers use the same batch counts.
Runtime depends on the compiler and the machine.
AIHC WebAssembly configurations enable Wasmtime CLI and HTTP host imports.
The AIHC runtime requires both sets of imports.

Each batch reads its inputs from an IORef.
This read prevents the loop from reusing an earlier computation result.
Each batch forces all result comparisons before the next batch.
Any incorrect result produces `fail` and a nonzero exit status.
All correct results produce `ok` and a zero exit status.

The table gives median elapsed times for three runs after one initial run.
Other compiler tasks used the machine during these measurements.

| Profile | RSA batches | RSA seconds | Hull batches | Hull seconds |
| --- | ---: | ---: | ---: | ---: |
| O0 | 4 | 0.622 | 184 | 0.504 |
| Os | 4 | 0.487 | 277 | 0.494 |
| O1 | 5 | 0.588 | 205 | 0.486 |
| O2 | 5 | 0.480 | 491 | 0.529 |
