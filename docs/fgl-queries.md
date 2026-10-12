# Graph queries with fgl

`fgl-queries-v1` runs five algorithms from the `fgl` library on fixed graphs.
It exercises `fgl` itself and the `containers`, `array` and `transformers` packages beneath it.

## Why no query repeats

A benchmark that repeats one computation measures whatever the compiler makes of the repetition.
A whole-program optimizer may compute the result once and reuse it, even across an `IORef`.
This benchmark never runs the same computation twice.
Every query in a family has a different input, and the profile sizes select a prefix of each family.
A slower profile runs fewer distinct queries, never the same query more often.

## The graphs

The main graph has 300 nodes and 1,500 directed edges with weights from 1 to 100.
It has no self loops and no duplicate edges.
The flow network has 100 nodes and 500 directed edges with capacities from 1 to 100.
It has no self loops, no duplicate edges and no antiparallel edges.
Both graphs are fixed lists in the `Fixtures` module.
The benchmark builds them as `PatriciaTree` graphs once.

## The five query families

Each family targets 0.1 seconds with native AIHC at -O2 on an Apple M4 Pro.

| Family | fgl function | Query | Answer compared |
| --- | --- | --- | --- |
| Shortest paths | `spTree` (Dijkstra, heap based) | A source node | Sum of (node + 1) times its distance over the tree |
| Levels | `level` (breadth-first search, queue based) | A source node | Sum of (node + 1) times its depth |
| Dominators | `iDom` (Cooper, Harvey and Kennedy on arrays) | A root node | Sum of (node + 1) times (immediate dominator + 1) |
| Components | `scc` after `delNode` (depth-first search) | A removed node | The component count plus the sum of size times (least node + 1) |
| Flows | `maxFlow` (Edmonds-Karp, rebuilds the residual graph) | A source and sink pair | The flow value |

The source, root and removed node orders are fixed shuffles of the node set.
The flow pairs are fixed pairs with a positive flow.
Every answer is checked against the fixture value.
Any mismatch produces `fail` and the family name with a nonzero exit status.
All correct answers produce `ok` and a zero exit status.

## Fixed reference answers

`scripts/fgl-fixtures.py` creates the graphs and every reference answer from a fixed seed.
Its reference algorithms are independent of `fgl`: a heap Dijkstra, a queue breadth-first search, the iterative dominator algorithm, an iterative Tarjan and an Edmonds-Karp on a capacity matrix.
`tests/test_fgl_fixtures.py` checks the fixture file against the script, checks that no family holds a query twice, and checks the reference algorithms on a graph with known answers.

To recreate the fixtures, execute this command from the benchmarks repository root:

```sh
python3 scripts/fgl-fixtures.py
```

To check the fixtures, execute this command:

```sh
python3 scripts/fgl-fixtures.py --check
```

## Runtime target

The profile sizes target 0.1 seconds per family, 0.5 seconds in all, with native AIHC on an Apple M4 Pro.
The reference compiler is the `aihc` package of the benchmarks flake on 2026-10-12.
The table gives the query counts and the median elapsed time of three runs after one initial run.

| Profile | Sources | Levels | Roots | Removed | Pairs | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| O0 | 18 | 13 | 2 | 7 | 1 | 0.44 |
| Os | 44 | 27 | 5 | 14 | 2 | 0.45 |
| O1 | 23 | 17 | 3 | 9 | 1 | 0.48 |
| O2 | 140 | 65 | 18 | 38 | 11 | 0.53 |

The dominator and flow families are coarse at -O0 and -O1.
One dominator tree costs about 50 milliseconds there and one flow about 100 milliseconds.
Both compilers use the same query counts.
