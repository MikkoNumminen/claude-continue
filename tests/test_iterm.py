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

    def test_text_is_escaped(self):
        s = iterm.build_applescript('say "hi"\\x', ["claude"], dry_run=False)
        self.assertIn(r'\"hi\"', s)
        self.assertNotIn('text "say "hi', s)  # the raw quote must not leak


if __name__ == "__main__":
    unittest.main()
