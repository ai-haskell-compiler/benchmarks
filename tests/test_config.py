import json
import tempfile
import unittest
from pathlib import Path

from aihc_bench.config import experiment_id, load_config


class ConfigTests(unittest.TestCase):
    def test_benchmark_source_content_changes_experiment_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Main.hs"
            config_path = root / "benchmark.json"
            config_path.write_text(json.dumps({
                "schema_version": 1,
                "suite_id": "test",
                "optimization": "O2",
                "measurement": {"relative_threshold": 0.01, "maximum_bucket_size": 4},
                "platforms": {"aarch64-darwin": {}},
                "benchmarks": [{"id": "sample", "source": "Main.hs", "expected_stdout": "ok\n"}],
                "configurations": [{"id": "config"}],
            }), encoding="utf-8")
            source.write_text("first", encoding="utf-8")
            first = experiment_id(load_config(config_path))
            source.write_text("second", encoding="utf-8")
            second = experiment_id(load_config(config_path))
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
