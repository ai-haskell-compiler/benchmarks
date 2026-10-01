import struct
import unittest

from aihc_bench.counters import CounterSession, open_session, perf_event_attr, scheduled_count


class PerfEventAttrTests(unittest.TestCase):
    def test_is_the_version_zero_layout(self):
        """64 bytes, which every kernel with perf_event_open accepts."""
        attr = perf_event_attr(1)
        self.assertEqual(len(attr), 64)
        kind, size, config = struct.unpack_from("=IIQ", attr)
        self.assertEqual((kind, size, config), (0, 64, 1))

    def test_counts_inherited_user_space_from_exec(self):
        flags = struct.unpack_from("=Q", perf_event_attr(0), 40)[0]
        disabled, inherit, exclude_kernel, enable_on_exec = 1, 1 << 1, 1 << 5, 1 << 12
        for bit in (disabled, inherit, exclude_kernel, enable_on_exec):
            self.assertTrue(flags & bit, hex(bit))

    def test_a_multiplexed_count_is_no_count(self):
        """An extrapolated count would look measured without being so."""
        self.assertEqual(scheduled_count(struct.pack("=QQQ", 900, 50, 50)), 900)
        self.assertIsNone(scheduled_count(struct.pack("=QQQ", 900, 50, 25)))


class SessionTests(unittest.TestCase):
    def test_a_session_always_opens(self):
        with open_session() as session:
            self.assertIsInstance(session, CounterSession)

    def test_the_null_session_counts_nothing(self):
        session = CounterSession()
        session.wait_exited(1)
        self.assertIsNone(session.read(1))


if __name__ == "__main__":
    unittest.main()
