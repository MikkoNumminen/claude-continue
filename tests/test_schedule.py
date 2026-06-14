import unittest
from datetime import timedelta

import _support  # noqa: F401
from _support import utc

from claude_continue import schedule
from claude_continue.model import Block


def _block(end):
    return Block(id="b", start=end - timedelta(hours=5), end=end, actual_end=None,
                 is_active=True, is_gap=False)


class TestNextTarget(unittest.TestCase):
    def test_reset_plus_buffer(self):
        end = utc(2026, 6, 14, 6)
        self.assertEqual(schedule.next_target(_block(end), 90), end + timedelta(seconds=90))


class TestFixedTarget(unittest.TestCase):
    def test_at_today_if_future(self):
        now = utc(2026, 6, 14, 5).astimezone()  # local
        # pick a time clearly after `now` in local terms
        later = (now + timedelta(hours=2)).strftime("%H:%M")
        target = schedule.fixed_target(now, at=later)
        self.assertEqual(target.strftime("%H:%M"), later)
        self.assertGreater(target, now)
        self.assertLess(target - now, timedelta(days=1))

    def test_at_rolls_to_tomorrow_if_past(self):
        now = utc(2026, 6, 14, 12).astimezone()
        earlier = (now - timedelta(hours=2)).strftime("%H:%M")
        target = schedule.fixed_target(now, at=earlier)
        self.assertGreater(target, now)
        self.assertGreaterEqual(target - now, timedelta(hours=21))

    def test_every_with_anchor(self):
        # anchor 06:00 local, every 5h, now 17:30 local -> next is 21:00 local
        now = utc(2026, 6, 14, 0).astimezone().replace(hour=17, minute=30, second=0, microsecond=0)
        target = schedule.fixed_target(now, every_hours=5, anchor="06:00")
        self.assertGreater(target, now)
        self.assertEqual(target.strftime("%H:%M"), "21:00")

    def test_every_requires_positive(self):
        now = utc(2026, 6, 14, 12).astimezone()
        with self.assertRaises(ValueError):
            schedule.fixed_target(now, every_hours=0)

    def test_requires_at_or_every(self):
        with self.assertRaises(ValueError):
            schedule.fixed_target(utc(2026, 6, 14, 12).astimezone())


class TestParseHhmm(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(schedule.parse_hhmm("09:05"), (9, 5))

    def test_invalid(self):
        for bad in ["9", "9:5:5", "25:00", "09:61", "ab:cd"]:
            with self.assertRaises(ValueError):
                schedule.parse_hhmm(bad)


if __name__ == "__main__":
    unittest.main()
