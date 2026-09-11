import json
import tempfile
import unittest

from aihc_bench.stats import StatsError, parse_aihc_json, parse_ghc_machine_readable, read_stats_file

GHC_OUTPUT = """'./prog' +RTS '-tstats.txt' '--machine-readable'
 [("bytes allocated", "11200352")
 ,("num_GCs", "3")
 ,("max_bytes_used", "36024")
 ,("GC_cpu_seconds", "0.000287")
 ,("allocated_bytes", "11200352")
 ,("max_live_bytes", "36024")
 ,("gen_0_collections", "2")
 ]
"""


class StatsTests(unittest.TestCase):
    def test_parses_ghc_machine_readable_pairs(self):
        stats = parse_ghc_machine_readable(GHC_OUTPUT)
        self.assertEqual(stats, {"peak_heap_bytes": 36024, "allocated_bytes": 11200352, "gc_count": 3, "gc_time_ns": 287000})

    def test_rejects_ghc_output_without_pairs(self):
        with self.assertRaises(StatsError):
            parse_ghc_machine_readable("nothing here")

    def test_parses_aihc_json(self):
        text = json.dumps({"schema": 1, "peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2, "gc_time_ns": 5})
        self.assertEqual(parse_aihc_json(text), {"peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2, "gc_time_ns": 5})
        with self.assertRaises(StatsError):
            parse_aihc_json(json.dumps({"schema": 2}))

    def test_missing_or_empty_file_means_no_stats(self):
        self.assertIsNone(read_stats_file("/nonexistent/stats", "ghc"))
        with tempfile.NamedTemporaryFile("w", suffix=".stats") as handle:
            self.assertIsNone(read_stats_file(handle.name, "ghc"))
            handle.write(GHC_OUTPUT)
            handle.flush()
            self.assertEqual(read_stats_file(handle.name, "ghc")["gc_count"], 3)


if __name__ == "__main__":
    unittest.main()
