import subprocess
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import winterm


class TestSendKeysEscaping(unittest.TestCase):
    def test_specials_wrapped(self):
        self.assertEqual(winterm._escape_sendkeys("a+b(c)"), "a{+}b{(}c{)}")

    def test_plain_text_unchanged(self):
        self.assertEqual(winterm._escape_sendkeys("continue"), "continue")

    def test_ps_quote_doubles_single_quotes(self):
        self.assertEqual(winterm._ps_quote("it's"), "it''s")


class TestBuildScript(unittest.TestCase):
    def test_targets_window_and_sends_enter(self):
        s = winterm.build_script("continue", "My Term")
        self.assertIn("AppActivate('My Term')", s)
        self.assertIn("continue{ENTER}", s)
        self.assertIn("SendKeys", s)

    def test_special_text_escaped_in_script(self):
        s = winterm.build_script("a(b)", "Windows Terminal")
        self.assertIn("a{(}b{)}{ENTER}", s)


class TestSelectWindows(unittest.TestCase):
    def test_window_title_is_the_target(self):
        out = winterm.select_windows(["claude — Windows Terminal"], ["claude"], "Windows Terminal")
        self.assertEqual(out, [("claude — Windows Terminal", "target")])

    def test_target_matching_is_case_insensitive(self):
        out = winterm.select_windows(["my WINDOWS TERMINAL"], [], "Windows Terminal")
        self.assertEqual(out, [("my WINDOWS TERMINAL", "target")])

    def test_filter_match_when_not_the_target(self):
        out = winterm.select_windows(["✳ claude in cmd"], ["claude", "✳"], "Windows Terminal")
        self.assertEqual(out, [("✳ claude in cmd", "match")])

    def test_non_matching_titles_dropped(self):
        out = winterm.select_windows(["Spotify", "Notepad"], ["claude"], "Windows Terminal")
        self.assertEqual(out, [])

    def test_targets_come_before_matches(self):
        out = winterm.select_windows(
            ["✳ claude (cmd)", "claude — Windows Terminal"], ["claude"], "Windows Terminal")
        self.assertEqual([s for _t, s in out], ["target", "match"])

    def test_duplicate_titles_collapsed(self):
        out = winterm.select_windows(
            ["Windows Terminal", "Windows Terminal"], [], "Windows Terminal")
        self.assertEqual(len(out), 1)

    def test_empty_window_title_still_lists_filter_matches(self):
        out = winterm.select_windows(["claude here"], ["claude"], "")
        self.assertEqual(out, [("claude here", "match")])

    def test_excluded_title_dropped_even_if_it_matches(self):
        # the GUI excludes its own "claude-continue" window so it isn't listed as
        # a candidate terminal just because the title contains "claude".
        out = winterm.select_windows(
            ["claude-continue", "✳ claude (cmd)"], ["claude"], "WT", exclude=("claude-continue",))
        self.assertEqual(out, [("✳ claude (cmd)", "match")])


class TestListWindows(unittest.TestCase):
    def test_parse_titles_strips_blanks(self):
        self.assertEqual(winterm._parse_titles("a\n\n  b  \n"), ["a", "b"])

    def test_build_list_script_uses_get_process(self):
        self.assertIn("Get-Process", winterm.build_list_script())
        self.assertIn("MainWindowTitle", winterm.build_list_script())

    def test_list_windows_with_injected_runner(self):
        out = winterm.list_windows(
            ["claude"], window_title="Windows Terminal",
            run=lambda timeout: "claude — Windows Terminal\nSpotify\n✳ claude (cmd)\n")
        self.assertEqual(out, [("claude — Windows Terminal", "target"), ("✳ claude (cmd)", "match")])

    def test_run_list_nonzero_raises(self):
        fail = subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch("claude_continue.winterm._powershell_bin", return_value="powershell"), \
             mock.patch("claude_continue.winterm.subprocess.run", return_value=fail):
            with self.assertRaises(RuntimeError):
                winterm._run_list(30.0)


class TestSendKeystroke(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        out = winterm.send_keystroke("continue", window_title="WT", dry_run=True)
        self.assertEqual(len(out), 1)
        self.assertIn("continue", out[0])
        self.assertIn("WT", out[0])

    def test_success_returns_label(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("claude_continue.winterm._powershell_bin", return_value="powershell"), \
             mock.patch("claude_continue.winterm.subprocess.run", return_value=ok):
            out = winterm.send_keystroke("continue", window_title="WT")
        self.assertTrue(out and "WT" in out[0])

    def test_nonzero_exit_raises(self):
        fail = subprocess.CompletedProcess([], 1, "", "window not found")
        with mock.patch("claude_continue.winterm._powershell_bin", return_value="powershell"), \
             mock.patch("claude_continue.winterm.subprocess.run", return_value=fail):
            with self.assertRaises(RuntimeError):
                winterm.send_keystroke("continue")


if __name__ == "__main__":
    unittest.main()
