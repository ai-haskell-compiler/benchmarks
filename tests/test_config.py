import json
import shutil
import tempfile
import unittest
from pathlib import Path

from aihc_bench.config import ConfigError, benchmark_experiment_id, experiment_ids, load_config, suite_key


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
            first = experiment_ids(load_config(config_path))
            main_file.write_text("second", encoding="utf-8")
            second = experiment_ids(load_config(config_path))
            self.assertNotEqual(first["sample"], second["sample"])
            self.assertTrue(first["sample"].startswith("sample-"))

    def test_adding_a_benchmark_keeps_the_other_experiments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = write_config(root, BASE)
            before = load_config(config_path)
            first = experiment_ids(before)
            shutil.copytree(root / "sample", root / "other")
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["benchmarks"].append({"id": "other", "package": "sample", "source": "other", "expected_stdout": "ok\n"})
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            after = load_config(config_path)
            second = experiment_ids(after)
            self.assertEqual(second["sample"], first["sample"])
            self.assertEqual(list(second), ["sample", "other"])
            self.assertNotEqual(suite_key(before), suite_key(after))
            self.assertTrue(suite_key(after).startswith("test-"))
            # The suite key depends only on the set of experiments, not their order.
            raw["benchmarks"].reverse()
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(suite_key(load_config(config_path)), suite_key(after))

    def test_configurations_change_every_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(write_config(root, BASE))
            benchmark = config["benchmarks"][0]
            first = benchmark_experiment_id(config, benchmark)
            config["configurations"][0]["compile"] = ["ghc", "-O2"]
            self.assertNotEqual(first, benchmark_experiment_id(config, benchmark))

    def test_configurations_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sample_package(root)
            for broken in (
                {**BASE, "optimization": "O3"},
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
        self.assertEqual(profiles, {(family, profile) for family in ("aihc", "ghc") for profile in ("O0", "O1", "O2", "Os")})
        baselines = {(item["backend"], item["optimization"]) for item in config["configurations"] if item.get("baseline")}
        self.assertEqual(baselines, {(backend, profile) for backend in ("native", "llvm", "wasm") for profile in ("O0", "O1", "O2", "Os")})
        for item in config["configurations"]:
            self.assertIn(item.get("runtime_stats"), {"ghc", "aihc"})
            # Every backend builds the benchmark's Cabal package through the
            # dependency-aware scripts, so a Hackage dependency such as
            # snappy-hs is compiled for Wasm exactly like for native code.
            if item["compiler_family"] == "ghc":
                self.assertNotIn("nix", item["compile"])
                self.assertEqual(item["compile"][0], "python3")
                self.assertIn("compile_with_cabal.py", item["compile"][1])
                ghc = item["compile"][item["compile"].index("--ghc") + 1]
                self.assertTrue(ghc.startswith("{toolchains}/bin/ghc-"))
                self.assertEqual(ghc.endswith("-wasm"), item["backend"] == "wasm")
                self.assertIn("--ghc-option=-rtsopts", item["compile"])
                self.assertEqual(item["compile"][item["compile"].index("--optimization") + 1], item["optimization"])
            if item["compiler_family"] == "aihc":
                expected = {"O0": ["optimization-flag"], "O1": ["optimization-O1"], "O2": [], "Os": ["optimization-Os"]}
                self.assertEqual(item["requires"], expected[item["optimization"]])
                self.assertEqual(item["compile"][0], "python3")
                self.assertIn("compile_with_aihc.py", item["compile"][1])
                self.assertEqual(item["compile"][item["compile"].index("--target") + 1], item["aihc_target"])
                if item["optimization"] == "O2":
                    self.assertNotIn("--optimization", item["compile"])
                    self.assertFalse(any(part.startswith("-O") for part in item["compile"]))
                else:
                    self.assertEqual(item["compile"][item["compile"].index("--optimization") + 1], item["optimization"])


if __name__ == "__main__":
    unittest.main()
