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
 ,("gen_0_max_pause_seconds", "0.000097")
 ,("gen_1_collections", "1")
 ,("gen_1_max_pause_seconds", "0.000731")
 ]
"""


class StatsTests(unittest.TestCase):
    def test_parses_ghc_machine_readable_pairs(self):
        stats = parse_ghc_machine_readable(GHC_OUTPUT)
        self.assertEqual(
            stats,
            {"peak_heap_bytes": 36024, "allocated_bytes": 11200352, "gc_count": 3, "gc_time_ns": 287000, "gc_max_pause_ns": 731000},
        )

    def test_ghc_without_pause_fields_reports_the_rest(self):
        without = "\n".join(line for line in GHC_OUTPUT.splitlines() if "max_pause" not in line)
        stats = parse_ghc_machine_readable(without)
        self.assertNotIn("gc_max_pause_ns", stats)
        self.assertEqual(stats["gc_count"], 3)

    def test_rejects_ghc_output_without_pairs(self):
        with self.assertRaises(StatsError):
            parse_ghc_machine_readable("nothing here")

    def test_parses_aihc_json(self):
        text = json.dumps({"schema": 1, "peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2, "gc_time_ns": 5})
        self.assertEqual(parse_aihc_json(text), {"peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2, "gc_time_ns": 5})
        with self.assertRaises(StatsError):
            parse_aihc_json(json.dumps({"schema": 1}))

    def test_parses_the_schema_3_object_the_runtime_writes(self):
        """The record of ``integer-fibonacci`` at ``-O2``, written on an Apple M1."""
        text = (
            '{"schema": 3, "peak_heap_bytes": 60577184, "allocated_bytes": 1551688536, "gc_count": 372, '
            '"gc_time_ns": 357106000, "gc_max_pause_ns": 18995000, "live_bytes": 8469992, '
            '"gc_minor_count": 294, "gc_gen1_count": 78, "gc_full_count": 78}'
        )
        self.assertEqual(
            parse_aihc_json(text),
            {
                "peak_heap_bytes": 60577184,
                "allocated_bytes": 1551688536,
                "gc_count": 372,
                "gc_time_ns": 357106000,
                "gc_max_pause_ns": 18995000,
            },
        )

    def test_schema_2_object_keeps_its_longest_pause(self):
        text = json.dumps({"schema": 2, "peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2, "gc_time_ns": 5, "gc_max_pause_ns": 3})
        self.assertEqual(parse_aihc_json(text)["gc_max_pause_ns"], 3)

    def test_reads_a_stats_file_from_the_newer_runtime(self):
        """The runner left every AIHC GC metric unavailable while this file raised."""
        record = {"schema": 3, "peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2, "gc_time_ns": 5, "gc_max_pause_ns": 3}
        with tempfile.NamedTemporaryFile("w", suffix=".stats") as handle:
            handle.write(json.dumps(record))
            handle.flush()
            self.assertEqual(read_stats_file(handle.name, "aihc")["gc_count"], 2)

    def test_aihc_json_may_report_its_longest_pause(self):
        """Optional, so a runtime that predates it still reports the rest."""
        text = json.dumps({"schema": 1, "peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2, "gc_time_ns": 5, "gc_max_pause_ns": 3})
        self.assertEqual(parse_aihc_json(text)["gc_max_pause_ns"], 3)
        with self.assertRaises(StatsError):
            parse_aihc_json(json.dumps({"schema": 1, "peak_heap_bytes": 10, "allocated_bytes": 20, "gc_count": 2}))

    def test_missing_or_empty_file_means_no_stats(self):
        self.assertIsNone(read_stats_file("/nonexistent/stats", "ghc"))
        with tempfile.NamedTemporaryFile("w", suffix=".stats") as handle:
            self.assertIsNone(read_stats_file(handle.name, "ghc"))
            handle.write(GHC_OUTPUT)
            handle.flush()
            self.assertEqual(read_stats_file(handle.name, "ghc")["gc_count"], 3)


if __name__ == "__main__":
    unittest.main()
