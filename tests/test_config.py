import json
import tempfile
import unittest
from pathlib import Path

from aihc_bench.config import ConfigError, experiment_id, load_config


def write_config(root, configuration):
    config_path = root / "benchmark.json"
    config_path.write_text(json.dumps({
        "schema_version": 2,
        "suite_id": "test",
        "measurement": {"relative_threshold": 0.01, "maximum_bucket_size": 4},
        "platforms": {"aarch64-darwin": {}},
        "benchmarks": [{"id": "sample", "source": "Main.hs", "expected_stdout": "ok\n"}],
        "configurations": [configuration],
    }), encoding="utf-8")
    return config_path


BASE = {
    "id": "config",
    "compiler_family": "ghc",
    "compiler_version": "9.14.1",
    "backend": "native",
    "gc": "ghc-rts",
    "optimization": "O2",
    "compile": ["ghc"],
    "run": ["{artifact}"],
}


class ConfigTests(unittest.TestCase):
    def test_benchmark_source_content_changes_experiment_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Main.hs"
            config_path = write_config(root, BASE)
            source.write_text("first", encoding="utf-8")
            first = experiment_id(load_config(config_path))
            source.write_text("second", encoding="utf-8")
            second = experiment_id(load_config(config_path))
            self.assertNotEqual(first, second)

    def test_configurations_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Main.hs").write_text("main", encoding="utf-8")
            for broken in (
                {**BASE, "optimization": "O1"},
                {**BASE, "runtime_stats": "rust"},
                {**BASE, "requires": ["teleport"]},
                {key: value for key, value in BASE.items() if key != "optimization"},
            ):
                with self.assertRaises(ConfigError):
                    load_config(write_config(root, broken))
            self.assertTrue(load_config(write_config(root, {**BASE, "runtime_stats": "ghc", "requires": ["build-exe"]})))

    def test_repository_configuration_loads(self):
        config = load_config(Path(__file__).resolve().parents[1] / "benchmark.json")
        profiles = {(item["compiler_family"], item["optimization"]) for item in config["configurations"]}
        self.assertEqual(profiles, {("aihc", "O0"), ("aihc", "O2"), ("ghc", "O0"), ("ghc", "O2")})
        baselines = {(item["backend"], item["optimization"]) for item in config["configurations"] if item.get("baseline")}
        self.assertEqual(baselines, {("native", "O0"), ("native", "O2"), ("llvm", "O0"), ("llvm", "O2"), ("wasm", "O0"), ("wasm", "O2")})
        for item in config["configurations"]:
            self.assertIn(item.get("runtime_stats"), {"ghc", "aihc"})
            if item["compiler_family"] == "ghc":
                self.assertIn("-rtsopts", item["compile"])
                self.assertTrue(item["compile"][0].startswith("{toolchains}/bin/ghc-"))
                self.assertNotIn("nix", item["compile"])
            if item["compiler_family"] == "aihc" and item["optimization"] == "O0":
                self.assertEqual(item["requires"], ["optimization-flag"])


if __name__ == "__main__":
    unittest.main()
