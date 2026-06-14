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
