import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from aihc_bench.schedule import (
    Schedule,
    Window,
    _linux_on_battery,
    current_window_end,
    in_window,
    next_window_start,
    parse_pmset,
)


class WindowTests(unittest.TestCase):
    def test_parses_every_spelling(self):
        for text in ("22:00-06:00", "22-6", "10pm-6am", "22:00 - 6am"):
            self.assertEqual(str(Window.parse(text)), "22:00-06:00", text)
        self.assertEqual(str(Window.parse("12am-12pm")), "00:00-12:00")
        self.assertEqual(str(Window.parse("9:30-24")), "09:30-00:00")

    def test_rejects_nonsense(self):
        for text in ("22:00", "25-6", "13pm-2am", "6-6", "a-b"):
            with self.assertRaises(ValueError, msg=text):
                Window.parse(text)

    def test_a_window_wraps_midnight(self):
        windows = [Window.parse("22:00-06:00")]
        self.assertTrue(in_window(windows, datetime(2026, 10, 10, 23, 0)))
        self.assertTrue(in_window(windows, datetime(2026, 10, 10, 5, 59)))
        self.assertFalse(in_window(windows, datetime(2026, 10, 10, 6, 0)))
        self.assertFalse(in_window(windows, datetime(2026, 10, 10, 21, 59)))

    def test_no_windows_means_any_time(self):
        now = datetime(2026, 10, 10, 12, 0)
        self.assertTrue(in_window([], now))
        self.assertIsNone(next_window_start([], now))

    def test_next_start_is_now_inside_and_the_nearest_start_outside(self):
        windows = [Window.parse("22:00-06:00"), Window.parse("12:00-13:00")]
        inside = datetime(2026, 10, 10, 12, 30)
        self.assertEqual(next_window_start(windows, inside), inside)
        self.assertEqual(next_window_start(windows, datetime(2026, 10, 10, 13, 0)), datetime(2026, 10, 10, 22, 0))
        self.assertEqual(next_window_start(windows, datetime(2026, 10, 10, 7, 0)), datetime(2026, 10, 10, 12, 0))

    def test_current_window_end(self):
        windows = [Window.parse("22:00-06:00")]
        self.assertEqual(current_window_end(windows, datetime(2026, 10, 10, 23, 0)), datetime(2026, 10, 11, 6, 0))
        self.assertEqual(current_window_end(windows, datetime(2026, 10, 11, 1, 0)), datetime(2026, 10, 11, 6, 0))
        self.assertIsNone(current_window_end(windows, datetime(2026, 10, 11, 12, 0)))


class ScheduleTests(unittest.TestCase):
    def test_add_remove_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule = Schedule(Path(directory) / "schedule.json")
            self.assertEqual(schedule.windows(), [])
            schedule.add(Window.parse("22-6"))
            schedule.add(Window.parse("22:00-06:00"))
            self.assertEqual(Schedule(schedule.path).windows(), [Window.parse("22-6")])
            schedule.remove(Window.parse("10pm-6am"))
            self.assertEqual(schedule.windows(), [])
            with self.assertRaises(ValueError):
                schedule.remove(Window.parse("1-2"))


class BatteryTests(unittest.TestCase):
    def test_pmset(self):
        self.assertFalse(parse_pmset("Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged"))
        self.assertTrue(parse_pmset("Now drawing from 'Battery Power'\n -InternalBattery-0 80%; discharging"))
        self.assertIsNone(parse_pmset(""))

    def _supplies(self, directory, supplies):
        for name, files in supplies.items():
            (directory / name).mkdir()
            for file, content in files.items():
                (directory / name / file).write_text(content + "\n")

    def test_linux(self):
        cases = [
            ({"AC": {"type": "Mains", "online": "1"}, "BAT0": {"type": "Battery", "status": "Charging"}}, False),
            ({"AC": {"type": "Mains", "online": "0"}, "BAT0": {"type": "Battery", "status": "Discharging"}}, True),
            ({"BAT0": {"type": "Battery", "status": "Discharging"}}, True),
            ({}, False),
            # A wireless mouse's battery says nothing about the machine.
            ({"hidpp_battery_0": {"type": "Battery", "scope": "Device", "status": "Discharging"}}, False),
        ]
        for supplies, expected in cases:
            with tempfile.TemporaryDirectory() as directory:
                self._supplies(Path(directory), supplies)
                self.assertEqual(_linux_on_battery(Path(directory)), expected, supplies)


if __name__ == "__main__":
    unittest.main()
