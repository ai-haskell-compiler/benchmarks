import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("integer_fixtures", ROOT / "scripts" / "integer-fixtures.py")
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


class IntegerFixtureTests(unittest.TestCase):
    def test_fixed_inputs_and_complete_reference_results(self):
        for name, create in (
            ("rsa-verify", FIXTURES.rsa_fixture),
            ("exact-convex-hull", FIXTURES.hull_fixture),
            ("arithmoi-factorise", FIXTURES.arithmoi_fixture),
        ):
            with self.subTest(name=name):
                actual = (ROOT / "benchmarks" / name / "Fixtures.hs").read_text()
                self.assertEqual(
                    "".join(FIXTURES.normalized(actual)),
                    "".join(FIXTURES.normalized(create())),
                )


if __name__ == "__main__":
    unittest.main()
