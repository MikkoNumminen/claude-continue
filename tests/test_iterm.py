import unittest

import _support  # noqa: F401

from claude_continue import iterm


class TestBuildApplescript(unittest.TestCase):
    def test_skip_busy_guard_present_by_default(self):
        s = iterm.build_applescript("continue", ["claude"], skip_busy=True)
        self.assertIn("(is processing of s) is false", s)

    def test_skip_busy_off_has_no_guard(self):
        s = iterm.build_applescript("continue", ["claude"], skip_busy=False)
        self.assertNotIn("is processing", s)

    def test_force_disables_skip_busy(self):
        s = iterm.build_applescript("continue", ["claude"], skip_busy=True, force=True)
        self.assertNotIn("is processing", s)

    def test_dry_run_does_not_write(self):
        s = iterm.build_applescript("continue", ["claude"], dry_run=True)
        self.assertNotIn("write text", s)
        self.assertIn("set end of firedNames", s)

    def test_non_dry_run_writes_text(self):
        s = iterm.build_applescript("continue", ["claude"], dry_run=False)
        self.assertIn('tell s to write text "continue"', s)

    def test_name_filter_or_clauses(self):
        s = iterm.build_applescript("continue", ["claude", "✳"])
        self.assertIn('sessionName contains "claude" or sessionName contains "✳"', s)

    def test_single_session_overrides_filter(self):
        s = iterm.build_applescript("continue", ["claude"], session="MyJob")
        self.assertIn('contains "MyJob"', s)
        self.assertNotIn('contains "claude"', s)

    def test_all_sessions_matches_true(self):
        s = iterm.build_applescript("continue", ["claude"], all_sessions=True)
        self.assertIn("if true then", s)

    def test_empty_filter_matches_nothing(self):
        s = iterm.build_applescript("continue", [])
        self.assertIn("if false then", s)

    def test_text_quote_is_escaped(self):
        s = iterm.build_applescript('say "hi"', ["claude"], dry_run=False)
        self.assertIn(r'\"hi\"', s)
        self.assertNotIn('text "say "hi', s)  # the raw quote must not leak

    def test_text_backslash_is_doubled(self):
        # backslash escaping is the load-bearing half of injection safety
        s = iterm.build_applescript("a\\b", ["claude"], dry_run=False)
        self.assertIn("a\\\\b", s)

    def test_as_str_orders_backslash_before_quote(self):
        # a value containing \" must become \\\"  (backslash doubled, then quote escaped)
        self.assertEqual(iterm._as_str('\\"'), '\\\\\\"')

    def test_session_name_with_quote_is_escaped(self):
        s = iterm.build_applescript("x", ["claude"], session='a"b')
        self.assertIn(r'contains "a\"b"', s)

    def test_filter_substring_with_quote_is_escaped(self):
        s = iterm.build_applescript("x", ['a"b'])
        self.assertIn(r'contains "a\"b"', s)


if __name__ == "__main__":
    unittest.main()
