import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aihc_bench.machine import derive_machine_id, load_machine, slug


class MachineTests(unittest.TestCase):
    def test_slug_keeps_three_meaningful_tokens(self):
        self.assertEqual(slug("Apple M4 Max"), "apple-m4-max")
        self.assertEqual(slug("AMD Ryzen 9 7950X 16-Core Processor"), "amd-ryzen-9")
        self.assertEqual(slug("Intel(R) Core(TM) i9-13900K CPU @ 3.00GHz"), "intel-i9-13900k")
        self.assertEqual(slug(""), "unknown-cpu")

    def test_machine_id_uses_hashed_identifier(self):
        first = derive_machine_id("Apple M4 Max", "uuid-one")
        second = derive_machine_id("Apple M4 Max", "uuid-two")
        self.assertTrue(first.startswith("apple-m4-max-"))
        self.assertEqual(len(first), len("apple-m4-max-") + 6)
        self.assertNotEqual(first, second)
        self.assertNotIn("uuid-one", first)

    def test_machine_record_is_frozen_on_first_use(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (
                patch("aihc_bench.machine.cpu_brand", return_value="Apple M4 Max"),
                patch("aihc_bench.machine.hardware_identifier", return_value=("uuid-one", "ioplatform-uuid")),
            ):
                record = load_machine(state)
            self.assertEqual(record["machine_id"], derive_machine_id("Apple M4 Max", "uuid-one"))
            self.assertEqual(record["derivation"]["identifier_source"], "ioplatform-uuid")

            with (
                patch("aihc_bench.machine.cpu_brand", return_value="Different CPU"),
                patch("aihc_bench.machine.hardware_identifier", return_value=("uuid-two", "ioplatform-uuid")),
            ):
                again = load_machine(state)
            self.assertEqual(again["machine_id"], record["machine_id"])
            stored = json.loads((state / "machine.json").read_text())
            self.assertEqual(stored["machine_id"], record["machine_id"])

    def test_override_replaces_and_freezes_the_id(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (
                patch("aihc_bench.machine.cpu_brand", return_value="Apple M4 Max"),
                patch("aihc_bench.machine.hardware_identifier", return_value=("host", "hostname")),
            ):
                load_machine(state)
                record = load_machine(state, override="build-box")
                self.assertEqual(record["machine_id"], "build-box")
                self.assertTrue(record["derivation"]["overridden"])
                self.assertEqual(load_machine(state)["machine_id"], "build-box")
                with self.assertRaises(ValueError):
                    load_machine(state, override="Not Valid")


if __name__ == "__main__":
    unittest.main()
