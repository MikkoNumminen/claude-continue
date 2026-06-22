import os
import time
import unittest
from datetime import datetime, timedelta

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

    def test_offset_defaults_to_zero(self):
        end = utc(2026, 6, 14, 6)
        self.assertEqual(schedule.next_target(_block(end), 90, 0), schedule.next_target(_block(end), 90))

    def test_positive_offset_pushes_fire_later(self):
        # a +42m correction for an estimate that runs early: reset + offset + buffer
        end = utc(2026, 6, 14, 6)
        self.assertEqual(schedule.next_target(_block(end), 90, 42 * 60),
                         end + timedelta(seconds=42 * 60 + 90))

    def test_negative_offset_pulls_fire_earlier(self):
        end = utc(2026, 6, 14, 6)
        self.assertEqual(schedule.next_target(_block(end), 90, -20 * 60),
                         end + timedelta(seconds=-20 * 60 + 90))


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

    def test_every_divisor_anchor_honored_daily(self):
        # 6h divides 24, so the anchor (06:00) is hit every day: grid 06/12/18/00
        now = utc(2026, 6, 14, 0).astimezone().replace(hour=17, minute=30, second=0, microsecond=0)
        target = schedule.fixed_target(now, every_hours=6, anchor="06:00")
        self.assertGreater(target, now)
        self.assertEqual(target.strftime("%H:%M"), "18:00")

    def test_every_divisor_anchor_holds_on_other_days(self):
        # for a divisor period the wall-clock fire times are the same set every day
        def local(y, mo, d, h, mi=0):
            return datetime(y, mo, d, h, mi).astimezone()
        allowed = {"01:00", "09:00", "17:00"}  # every 8h from 09:00
        for day in (5, 6, 7):
            t = schedule.fixed_target(local(2026, 3, day, 3, 0), every_hours=8, anchor="09:00")
            self.assertIn(t.strftime("%H:%M"), allowed)

    def test_every_cadence_is_continuous_across_days(self):
        # 7h does NOT divide 24: the grid must keep a constant 7h gap across the
        # day boundary, never restarting at the daily anchor (the bug we fixed).
        def local(y, mo, d, h, mi=0):
            return datetime(y, mo, d, h, mi).astimezone()

        # walk forward across midnight, each step from the previous fire instant
        t = local(2026, 1, 5, 0, 30)
        gaps = []
        prev = None
        for _ in range(8):
            t = schedule.fixed_target(t, every_hours=7, anchor="00:00")
            if prev is not None:
                gaps.append(round((t - prev).total_seconds() / 3600.0, 3))
            prev = t
        self.assertTrue(all(g == 7.0 for g in gaps), "non-constant gaps: %s" % gaps)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the timezone")
    def test_every_is_strictly_future_across_dst_gap(self):
        # A grid point inside the spring-forward gap must not resolve to <= now.
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            # 2026-03-08: clocks jump 02:00 -> 03:00, so 02:30 is nonexistent.
            now = datetime(2026, 3, 8, 1, 30).astimezone()
            target = schedule.fixed_target(now, every_hours=24, anchor="02:30")
            self.assertGreater(target, now)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

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
