import unittest
from pathlib import Path

from aihc_bench.freeze import parse_build_depends, parse_freeze, resolve_dependency_constraints

REPO_ROOT = Path(__file__).resolve().parents[1]


class FreezeTests(unittest.TestCase):
    def test_parses_snappy_roundtrip_freeze_and_depends(self):
        source = REPO_ROOT / "benchmarks" / "snappy-roundtrip"
        freeze = parse_freeze(source / "cabal.project.freeze")
        self.assertEqual(freeze["snappy-hs"], "0.1.2.0")
        self.assertIn("bytestring", freeze)

        depends = parse_build_depends(source / "snappy-roundtrip.cabal")
        self.assertEqual(depends, ["base", "bytestring", "snappy-hs"])

        constraints = resolve_dependency_constraints(source, freeze)
        self.assertEqual(constraints, [f"bytestring=={freeze['bytestring']}", f"snappy-hs=={freeze['snappy-hs']}"])

    def test_base_only_benchmark_has_no_dependency_constraints(self):
        source = REPO_ROOT / "benchmarks" / "integer-factorial"
        freeze = parse_freeze(source / "cabal.project.freeze")
        self.assertEqual(resolve_dependency_constraints(source, freeze), [])

    def test_missing_pin_raises(self):
        source = REPO_ROOT / "benchmarks" / "snappy-roundtrip"
        with self.assertRaises(KeyError):
            resolve_dependency_constraints(source, {})


if __name__ == "__main__":
    unittest.main()
