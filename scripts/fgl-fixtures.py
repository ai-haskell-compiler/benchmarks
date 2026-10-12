#!/usr/bin/env python3
"""Create the fixed graphs and reference answers of the fgl benchmark."""

import argparse
import heapq
import random
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "benchmarks"


def simple_edges(rng, nodes, count, antiparallel):
    """Return distinct weighted edges without self loops.

    With ``antiparallel`` False no edge has its reverse in the set, which is
    what the max flow reference assumes.
    """
    edges = {}
    while len(edges) < count:
        a = rng.randrange(nodes)
        b = rng.randrange(nodes)
        if a == b or (a, b) in edges or (not antiparallel and (b, a) in edges):
            continue
        edges[(a, b)] = 1 + rng.randrange(100)
    return sorted((a, b, w) for (a, b), w in edges.items())


def successors(nodes, edges):
    table = [[] for _ in range(nodes)]
    for a, b, w in edges:
        table[a].append((b, w))
    return table


def shortest_path_sum(table, source):
    """Sum (node + 1) * distance over the nodes Dijkstra reaches."""
    distance = {}
    queue = [(0, source)]
    while queue:
        d, v = heapq.heappop(queue)
        if v in distance:
            continue
        distance[v] = d
        for u, w in table[v]:
            if u not in distance:
                heapq.heappush(queue, (d + w, u))
    return sum((v + 1) * d for v, d in distance.items())


def level_sum(table, source):
    """Sum (node + 1) * depth over the nodes breadth-first search reaches."""
    depth = {source: 0}
    queue = deque([source])
    while queue:
        v = queue.popleft()
        for u, _ in table[v]:
            if u not in depth:
                depth[u] = depth[v] + 1
                queue.append(u)
    return sum((v + 1) * d for v, d in depth.items())


def dominator_sum(table, root):
    """Sum (node + 1) * (immediate dominator + 1) over the nodes the root reaches.

    This is the iterative algorithm of Cooper, Harvey and Kennedy.
    """
    order = []
    seen = {root}
    stack = [(root, iter(table[root]))]
    while stack:
        v, children = stack[-1]
        for u, _ in children:
            if u not in seen:
                seen.add(u)
                stack.append((u, iter(table[u])))
                break
        else:
            stack.pop()
            order.append(v)
    order.reverse()
    index = {v: i for i, v in enumerate(order)}
    predecessors = {v: [] for v in order}
    for v in order:
        for u, _ in table[v]:
            if u in index:
                predecessors[u].append(v)
    idom = {root: root}

    def intersect(a, b):
        while a != b:
            while index[a] > index[b]:
                a = idom[a]
            while index[b] > index[a]:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for v in order[1:]:
            candidates = [p for p in predecessors[v] if p in idom]
            new = candidates[0]
            for p in candidates[1:]:
                new = intersect(p, new)
            if idom.get(v) != new:
                idom[v] = new
                changed = True
    return sum((v + 1) * (d + 1) for v, d in idom.items() if v != root)


def component_sum(nodes, edges, removed):
    """Count the strongly connected components of the graph without one node.

    The result adds the count to the sum of size * (least node + 1) over the
    components. Tarjan's algorithm is run iteratively.
    """
    table = [[] for _ in range(nodes)]
    for a, b, _ in edges:
        if removed not in (a, b):
            table[a].append(b)
    index = {}
    low = {}
    on_stack = set()
    stack = []
    result = 0
    for start in range(nodes):
        if start == removed or start in index:
            continue
        work = [(start, 0)]
        while work:
            v, position = work[-1]
            if v not in index:
                index[v] = low[v] = len(index)
                stack.append(v)
                on_stack.add(v)
            advanced = False
            while position < len(table[v]):
                u = table[v][position]
                position += 1
                if u not in index:
                    work[-1] = (v, position)
                    work.append((u, 0))
                    advanced = True
                    break
                if u in on_stack:
                    low[v] = min(low[v], index[u])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                component = []
                while True:
                    u = stack.pop()
                    on_stack.discard(u)
                    component.append(u)
                    if u == v:
                        break
                result += len(component) * (min(component) + 1)
                result += 1
    return result


def max_flow(nodes, edges, source, sink):
    """Edmonds-Karp on a network without antiparallel edges."""
    capacity = [[0] * nodes for _ in range(nodes)]
    neighbours = [[] for _ in range(nodes)]
    for a, b, w in edges:
        capacity[a][b] = w
        neighbours[a].append(b)
        neighbours[b].append(a)
    total = 0
    while True:
        parent = {source: None}
        queue = deque([source])
        while queue and sink not in parent:
            v = queue.popleft()
            for u in neighbours[v]:
                if u not in parent and capacity[v][u] > 0:
                    parent[u] = v
                    queue.append(u)
        if sink not in parent:
            return total
        bottleneck = None
        v = sink
        while parent[v] is not None:
            edge = capacity[parent[v]][v]
            bottleneck = edge if bottleneck is None else min(bottleneck, edge)
            v = parent[v]
        v = sink
        while parent[v] is not None:
            capacity[parent[v]][v] -= bottleneck
            capacity[v][parent[v]] += bottleneck
            v = parent[v]
        total += bottleneck


GRAPH_NODES = 300
GRAPH_EDGES = 1500
FLOW_NODES = 100
FLOW_EDGES = 500
FLOW_PAIRS = 48


def fgl_fixture():
    rng = random.Random(20261012)
    edges = simple_edges(rng, GRAPH_NODES, GRAPH_EDGES, True)
    flow_edges = simple_edges(rng, FLOW_NODES, FLOW_EDGES, False)
    table = successors(GRAPH_NODES, edges)
    sources = list(range(GRAPH_NODES))
    rng.shuffle(sources)
    roots = list(range(GRAPH_NODES))
    rng.shuffle(roots)
    removed = list(range(GRAPH_NODES))
    rng.shuffle(removed)
    pairs = []
    while len(pairs) < FLOW_PAIRS:
        s = rng.randrange(FLOW_NODES)
        t = rng.randrange(FLOW_NODES)
        value = max_flow(FLOW_NODES, flow_edges, s, t) if s != t else 0
        if value > 0 and (s, t) not in [pair for pair, _ in pairs]:
            pairs.append(((s, t), value))
    tables = (
        ("shortestPaths", "[(Int, Int)]", [(s, shortest_path_sum(table, s)) for s in sources]),
        ("levels", "[(Int, Int)]", [(s, level_sum(table, s)) for s in sources]),
        ("dominators", "[(Int, Int)]", [(r, dominator_sum(table, r)) for r in roots]),
        ("components", "[(Int, Int)]", [(v, component_sum(GRAPH_NODES, edges, v)) for v in removed]),
        ("flows", "[((Int, Int), Int)]", pairs),
    )
    render = lambda rows: "[\n  " + ",\n  ".join(str(row).replace("'", "") for row in rows) + "\n  ]"
    text = (
        "-- Create this file with scripts/fgl-fixtures.py.\n"
        "module Fixtures\n"
        "  ( graphNodes, graphEdges, flowNodes, flowEdges,\n"
        "    shortestPaths, levels, dominators, components, flows,\n"
        "  ) where\n\n"
        f"graphNodes :: Int\ngraphNodes = {GRAPH_NODES}\n\n"
        f"graphEdges :: [(Int, Int, Int)]\ngraphEdges = {render(edges)}\n\n"
        f"flowNodes :: Int\nflowNodes = {FLOW_NODES}\n\n"
        f"flowEdges :: [(Int, Int, Int)]\nflowEdges = {render(flow_edges)}\n"
    )
    for name, kind, rows in tables:
        text += f"\n-- | Each query and its reference answer.\n{name} :: {kind}\n{name} = {render(rows)}\n"
    return text


def normalized(text):
    return "".join(line.split("--", 1)[0] for line in text.splitlines()).split()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the fixed fixture file.")
    args = parser.parse_args()
    path = ROOT / "fgl-queries" / "Fixtures.hs"
    text = fgl_fixture()
    if args.check:
        assert "".join(normalized(path.read_text())) == "".join(normalized(text)), str(path)
    else:
        path.write_text(text)


if __name__ == "__main__":
    main()
