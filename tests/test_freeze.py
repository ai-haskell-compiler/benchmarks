import json
import unittest
from pathlib import Path

from aihc_bench.freeze import parse_build_depends, parse_freeze

REPO_ROOT = Path(__file__).resolve().parents[1]


class FreezeTests(unittest.TestCase):
    def test_parses_snappy_roundtrip_freeze_and_depends(self):
        source = REPO_ROOT / "benchmarks" / "snappy-roundtrip"
        freeze = parse_freeze(source / "cabal.project.freeze")
        self.assertEqual(freeze["snappy-hs"], "0.1.2.0")
        self.assertIn("bytestring", freeze)

        depends = parse_build_depends(source / "snappy-roundtrip.cabal")
        self.assertEqual(depends, ["base", "bytestring", "snappy-hs"])

    def test_base_only_benchmark_depends_on_base_alone(self):
        source = REPO_ROOT / "benchmarks" / "integer-factorial"
        self.assertEqual(parse_build_depends(source / "integer-factorial.cabal"), ["base"])

    def test_non_boot_dependencies_are_pinned_in_the_cabal_file(self):
        """aihc build resolves from the .cabal, not the freeze file.

        Only an exact bound there makes both toolchains compile the same
        version. Boot libraries are exempt: each GHC ships its own, so a hard
        bound would break every release but the one the freeze file pins.
        """
        boot = set(json.loads((REPO_ROOT / "benchmark.json").read_text())["ghc_boot_libraries"])
        for cabal_file in REPO_ROOT.glob("benchmarks/*/*.cabal"):
            freeze = parse_freeze(cabal_file.parent / "cabal.project.freeze")
            text = cabal_file.read_text()
            for name in parse_build_depends(cabal_file):
                if name in boot:
                    continue
                self.assertIn(f"{name} =={freeze[name]}", text, f"{cabal_file.name} does not pin {name}")


if __name__ == "__main__":
    unittest.main()
