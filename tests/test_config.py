import json
import tempfile
import unittest
from pathlib import Path

from aihc_bench.config import ConfigError, experiment_id, load_config


def write_sample_package(root, main_contents="main = pure ()"):
    package = root / "sample"
    package.mkdir(exist_ok=True)
    (package / "sample.cabal").write_text(
        "cabal-version: 2.4\nname: sample\nversion: 0.1.0.0\nbuild-type: Simple\n\n"
        "executable sample\n  main-is: Main.hs\n  build-depends: base\n  default-language: Haskell2010\n",
        encoding="utf-8",
    )
    (package / "Main.hs").write_text(main_contents, encoding="utf-8")
    return package


def write_config(root, configuration):
    write_sample_package(root)
    config_path = root / "benchmark.json"
    config_path.write_text(json.dumps({
        "schema_version": 2,
        "suite_id": "test",
        "measurement": {"relative_threshold": 0.01, "maximum_bucket_size": 4},
        "platforms": {"aarch64-darwin": {}},
        "benchmarks": [{"id": "sample", "package": "sample", "source": "sample", "expected_stdout": "ok\n"}],
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
            config_path = write_config(root, BASE)
            main_file = root / "sample" / "Main.hs"
            main_file.write_text("first", encoding="utf-8")
            first = experiment_id(load_config(config_path))
            main_file.write_text("second", encoding="utf-8")
            second = experiment_id(load_config(config_path))
            self.assertNotEqual(first, second)

    def test_configurations_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sample_package(root)
            for broken in (
                {**BASE, "optimization": "O1"},
                {**BASE, "runtime_stats": "rust"},
                {**BASE, "requires": ["teleport"]},
                {key: value for key, value in BASE.items() if key != "optimization"},
            ):
                with self.assertRaises(ConfigError):
                    load_config(write_config(root, broken))
            self.assertTrue(load_config(write_config(root, {**BASE, "runtime_stats": "ghc", "requires": ["build-exe"]})))

    def test_aihc_since_must_be_a_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = write_config(root, BASE)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            for broken in ("yesterday", 20260901):
                config_path.write_text(json.dumps({**config, "aihc_since": broken}), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(config_path)
            config_path.write_text(json.dumps({**config, "aihc_since": "2026-09-01"}), encoding="utf-8")
            self.assertEqual(load_config(config_path)["aihc_since"], "2026-09-01")

    def test_repository_configuration_loads(self):
        config = load_config(Path(__file__).resolve().parents[1] / "benchmark.json")
        profiles = {(item["compiler_family"], item["optimization"]) for item in config["configurations"]}
        self.assertEqual(profiles, {("aihc", "O0"), ("aihc", "O2"), ("ghc", "O0"), ("ghc", "O2")})
        baselines = {(item["backend"], item["optimization"]) for item in config["configurations"] if item.get("baseline")}
        self.assertEqual(baselines, {("native", "O0"), ("native", "O2"), ("llvm", "O0"), ("llvm", "O2"), ("wasm", "O0"), ("wasm", "O2")})
        for item in config["configurations"]:
            self.assertIn(item.get("runtime_stats"), {"ghc", "aihc"})
            if item["compiler_family"] == "ghc":
                self.assertNotIn("nix", item["compile"])
                if item["backend"] == "wasm":
                    self.assertIn("-rtsopts", item["compile"])
                    self.assertTrue(item["compile"][0].startswith("{toolchains}/bin/ghc-"))
                else:
                    self.assertEqual(item["compile"][0], "python3")
                    self.assertIn("compile_with_cabal.py", item["compile"][1])
                    self.assertTrue(any(value.startswith("{toolchains}/bin/ghc-") for value in item["compile"]))
                    self.assertIn("--ghc-option=-rtsopts", item["compile"])
            if item["compiler_family"] == "aihc" and item["optimization"] == "O0":
                self.assertEqual(item["requires"], ["optimization-flag"])


if __name__ == "__main__":
    unittest.main()
