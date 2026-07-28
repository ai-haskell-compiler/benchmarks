import tempfile
import unittest
from pathlib import Path

from aihc_bench.catalog import build_bundle, merge_catalog
from aihc_bench.report import generate_summary


def envelope(platform="aarch64-darwin"):
    commit = {"sha": "a" * 40, "ordinal": 7, "committed_at": "2026-07-26T00:00:00Z", "subject": "faster"}
    results = []
    for benchmark in ["fib-v1", "fact-v1"]:
        for family, version, backend, gc, estimate in [
            ("aihc", commit["sha"], "native", "semispace", 80_000_000),
            ("aihc", commit["sha"], "wasm", "semispace", 90_000_000),
            ("ghc", "9.14.1", "native", "ghc-rts", 100_000_000),
        ]:
            results.append({
                "benchmark": benchmark, "configuration": f"{family}-{backend}", "compiler_family": family,
                "compiler_version": version, "backend": backend, "gc": gc, "optimization": "O2",
                "compile": {"status": "compiled"},
                "measurement": {"status": "converged", "metrics": [{"metric": "wall_time", "unit": "ns", "estimate": estimate, "samples": [estimate]}]},
            })
    return {
        "schema_version": 1, "run_id": "run-1", "created_at": "2026-07-26T00:01:00Z",
        "experiment_id": "test-exp", "platform": platform,
        "environment": {"id": f"{platform}-env", "hardware_model": "Test machine"},
        "aihc_commit": commit, "compiler_status": "available", "unavailable_reason": None, "results": results,
    }


class CatalogTests(unittest.TestCase):
    def test_bundle_and_readme_share_the_same_views(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog, uploads = build_bundle([envelope()], Path(directory))
            self.assertEqual(len(catalog["series"]), 2)
            self.assertTrue(uploads)
            summary = generate_summary(catalog)
            self.assertIn("80.00 ms · 0.800×", summary)
            self.assertIn("90.00 ms", summary)
            self.assertIn("— no GHC baseline", summary)
            self.assertIn("`aaaaaaaaaaaa`", summary)

    def test_merge_preserves_other_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apple, _ = build_bundle([envelope()], root / "apple")
            linux, _ = build_bundle([envelope("x86_64-linux")], root / "linux")
            merged = merge_catalog(apple, linux)
            self.assertEqual({item["platform"] for item in merged["revision_indexes"]}, {"aarch64-darwin", "x86_64-linux"})


if __name__ == "__main__":
    unittest.main()
