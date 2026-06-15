import unittest

import _support  # noqa: F401  (path setup)
from _support import fixture

from claude_continue.model import Block, active_block_from_payload, parse_iso


class TestParseIso(unittest.TestCase):
    def test_z_suffix_becomes_utc(self):
        dt = parse_iso("2026-06-14T06:00:00.000Z")
        self.assertEqual(dt.utcoffset().total_seconds(), 0)
        self.assertEqual(dt.hour, 6)

    def test_offset_normalised_to_utc(self):
        dt = parse_iso("2026-06-14T09:00:00+03:00")
        self.assertEqual(dt.hour, 6)  # converted to UTC
        self.assertEqual(dt.utcoffset().total_seconds(), 0)


class TestBlock(unittest.TestCase):
    def test_from_active_fixture(self):
        block = active_block_from_payload(fixture("active.json"))
        self.assertIsNotNone(block)
        self.assertTrue(block.is_active)
        self.assertFalse(block.is_gap)
        # end == start + 5h, and reset_at == end
        self.assertEqual((block.end - block.start).total_seconds(), 5 * 3600)
        self.assertEqual(block.reset_at, block.end)

    def test_idle_payload_returns_none(self):
        self.assertIsNone(active_block_from_payload(fixture("idle.json")))
        self.assertIsNone(active_block_from_payload({}))
        self.assertIsNone(active_block_from_payload({"blocks": []}))

    def test_recent_skips_gaps_finds_active(self):
        payload = fixture("recent.json")
        # the fixture is known to contain gap blocks
        self.assertTrue(any(b.get("isGap") for b in payload["blocks"]))
        block = active_block_from_payload(payload)
        self.assertIsNotNone(block)
        self.assertFalse(block.is_gap)
        self.assertTrue(block.is_active)

    def test_gap_block_is_never_returned(self):
        payload = {
            "blocks": [
                {"id": "gap-x", "startTime": "2026-06-11T05:00:00Z",
                 "endTime": "2026-06-11T07:00:00Z", "isGap": True, "isActive": True},
            ]
        }
        # even an (impossible) active gap must be skipped
        self.assertIsNone(active_block_from_payload(payload))

    def test_actual_end_optional(self):
        b = Block.from_json({
            "id": "x", "startTime": "2026-06-14T01:00:00Z",
            "endTime": "2026-06-14T06:00:00Z", "isActive": True, "isGap": False,
        })
        self.assertIsNone(b.actual_end)


if __name__ == "__main__":
    unittest.main()
