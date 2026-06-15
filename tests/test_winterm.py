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


class TestParseInstances(unittest.TestCase):
    def test_parses_pid_and_strips_exe(self):
        out = winterm.parse_instances("22108\tclaude.exe\n35552\tclaude.exe\n")
        self.assertEqual(out, [("claude", "22108"), ("claude", "35552")])

    def test_node_based_cli_kept_as_node(self):
        self.assertEqual(winterm.parse_instances("900\tnode.exe\n"), [("node", "900")])

    def test_blank_and_non_numeric_and_tabless_lines_dropped(self):
        # blank lines, a stray header row (non-numeric pid), and lines without a
        # tab are all ignored; only the real "<pid>\t<name>" row survives.
        out = winterm.parse_instances("\nProcessId\tName\n123\tclaude.exe\nno_tab\n")
        self.assertEqual(out, [("claude", "123")])

    def test_dedup_by_pid(self):
        out = winterm.parse_instances("123\tclaude.exe\n123\tclaude.exe\n")
        self.assertEqual(out, [("claude", "123")])


class TestListClaudeInstances(unittest.TestCase):
    def test_build_instances_script_matches_claude_processes(self):
        s = winterm.build_instances_script()
        self.assertIn("Win32_Process", s)
        self.assertIn("claude.exe", s)
        self.assertIn("ProcessId", s)
        # the claude-code command-line match is scoped to node.exe so the query's
        # own PowerShell process (whose command line contains "claude-code") and
        # other shells don't self-match.
        self.assertIn("node.exe", s)
        self.assertIn("claude-code", s)

    def test_list_with_injected_runner(self):
        out = winterm.list_claude_instances(
            run=lambda t: "22108\tclaude.exe\n35552\tclaude.exe\n")
        self.assertEqual(out, [("claude", "22108"), ("claude", "35552")])

    def test_empty_when_no_instances(self):
        self.assertEqual(winterm.list_claude_instances(run=lambda t: ""), [])

    def test_run_instances_nonzero_raises(self):
        fail = subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch("claude_continue.winterm._powershell_bin", return_value="powershell"), \
             mock.patch("claude_continue.winterm.subprocess.run", return_value=fail):
            with self.assertRaises(RuntimeError):
                winterm._run_instances(30.0)

    def test_run_instances_passes_no_window_flag_on_windows(self):
        # the GUI poll must not flash a console; _run_instances passes the
        # no-window creationflags from osenv on Windows.
        ok = subprocess.CompletedProcess([], 0, "", "")
        captured = {}

        def fake_run(*a, **kw):
            captured.update(kw)
            return ok

        with mock.patch("claude_continue.winterm._powershell_bin", return_value="powershell"), \
             mock.patch("claude_continue.winterm.osenv.no_window_kwargs", return_value={"creationflags": 0x08000000}), \
             mock.patch("claude_continue.winterm.subprocess.run", side_effect=fake_run):
            winterm._run_instances(30.0)
        self.assertEqual(captured.get("creationflags"), 0x08000000)


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
