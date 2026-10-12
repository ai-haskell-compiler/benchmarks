import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fgl_fixtures", ROOT / "scripts" / "fgl-fixtures.py")
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


class FglFixtureTests(unittest.TestCase):
    def test_fixed_graphs_and_complete_reference_answers(self):
        actual = (ROOT / "benchmarks" / "fgl-queries" / "Fixtures.hs").read_text()
        self.assertEqual(
            "".join(FIXTURES.normalized(actual)),
            "".join(FIXTURES.normalized(FIXTURES.fgl_fixture())),
        )

    def test_every_query_is_distinct(self):
        text = FIXTURES.fgl_fixture()
        for name in ("shortestPaths", "levels", "dominators", "components", "flows"):
            with self.subTest(name=name):
                body = text.split(f"\n{name} = [")[1].split("]")[0]
                queries = [line.rsplit(",", 1)[0].strip() for line in body.strip().splitlines()]
                self.assertEqual(len(queries), len(set(queries)))

    def test_reference_algorithms_agree_on_a_small_graph(self):
        edges = [(0, 1, 2), (1, 2, 3), (0, 2, 10), (2, 3, 1), (3, 1, 1)]
        table = FIXTURES.successors(4, edges)
        # Distances from 0: 0, 2, 5, 6.
        self.assertEqual(FIXTURES.shortest_path_sum(table, 0), 1 * 0 + 2 * 2 + 3 * 5 + 4 * 6)
        # Depths from 0: 0, 1, 1, 2.
        self.assertEqual(FIXTURES.level_sum(table, 0), 2 * 1 + 3 * 1 + 4 * 2)
        # Immediate dominators from 0: node 1 and 2 by 0, node 3 by 2.
        self.assertEqual(FIXTURES.dominator_sum(table, 0), 2 * 1 + 3 * 1 + 4 * 3)
        # Without node 0 the cycle 1 -> 2 -> 3 -> 1 is one component of three nodes.
        self.assertEqual(FIXTURES.component_sum(4, edges, 0), 1 + 3 * 2)
        # Without node 3 every node is its own component.
        self.assertEqual(FIXTURES.component_sum(4, edges, 3), 3 + 1 * 1 + 1 * 2 + 1 * 3)
        # Flow 0 -> 3 is limited by the single edge into 3.
        self.assertEqual(FIXTURES.max_flow(4, [(0, 1, 2), (1, 2, 3), (0, 2, 10), (2, 3, 1)], 0, 3), 1)
        self.assertEqual(FIXTURES.max_flow(4, [(0, 1, 2), (1, 2, 3), (0, 2, 10), (2, 3, 7)], 0, 3), 7)


if __name__ == "__main__":
    unittest.main()
