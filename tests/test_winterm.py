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

    def test_four_column_unrelated_parents_keeps_both(self):
        # "<pid>\t<ppid>\t<ctime>\t<name>" — two sessions whose parents are shells
        # (not in the matched set) are both kept.
        out = winterm.parse_instances(
            "40996\t34540\t1000\tclaude.exe\n16828\t22768\t1500\tclaude.exe\n")
        self.assertEqual(out, [("claude", "40996"), ("claude", "16828")])

    def test_worker_child_folded_onto_older_launcher(self):
        # the real shim pair: claude --resume (30880) is a child of claude
        # --continue (40996), created later and sharing its console -> one row.
        out = winterm.parse_instances(
            "40996\t34540\t1000\tclaude.exe\n30880\t40996\t2000\tclaude.exe\n")
        self.assertEqual(out, [("claude", "40996")])

    def test_fold_holds_when_child_listed_before_parent(self):
        # listing order must not matter — the worker can appear before its launcher.
        out = winterm.parse_instances(
            "30880\t40996\t2000\tclaude.exe\n40996\t34540\t1000\tclaude.exe\n")
        self.assertEqual(out, [("claude", "40996")])

    def test_two_sessions_plus_one_worker(self):
        # two real sessions; the first also has a worker child -> two rows total.
        out = winterm.parse_instances(
            "40996\t34540\t1000\tclaude.exe\n"
            "30880\t40996\t2000\tclaude.exe\n"   # worker of 40996 (folded)
            "16828\t22768\t1500\tclaude.exe\n")  # independent session (kept)
        self.assertEqual(out, [("claude", "40996"), ("claude", "16828")])

    def test_recycled_ppid_not_folded(self):
        # ppid 100 is in the matched set, but that process was created AFTER 5000
        # (5000's real parent shell exited and pid 100 was recycled to a new claude)
        # -> 5000 is a separate live session and must be kept, not folded away.
        out = winterm.parse_instances(
            "5000\t100\t1000\tclaude.exe\n100\t34540\t9000\tclaude.exe\n")
        self.assertEqual(out, [("claude", "5000"), ("claude", "100")])

    def test_unknown_creation_time_not_folded(self):
        # no creation times -> can't confirm parentage -> keep both (never drop a
        # live session on a guess).
        out = winterm.parse_instances(
            "40996\t34540\t\tclaude.exe\n30880\t40996\t\tclaude.exe\n")
        self.assertEqual(out, [("claude", "40996"), ("claude", "30880")])

    def test_one_known_one_unknown_ctime_not_folded(self):
        # docstring contract: when EITHER time is unknown, keep the row (never fold
        # on a guess) — both the child-unknown and parent-unknown directions.
        child_unknown = winterm.parse_instances(
            "40996\t34540\t1000\tclaude.exe\n30880\t40996\t\tclaude.exe\n")
        self.assertEqual(child_unknown, [("claude", "40996"), ("claude", "30880")])
        parent_unknown = winterm.parse_instances(
            "40996\t34540\t\tclaude.exe\n30880\t40996\t2000\tclaude.exe\n")
        self.assertEqual(parent_unknown, [("claude", "40996"), ("claude", "30880")])

    def test_non_ascii_digit_ctime_does_not_crash(self):
        # isdigit() is True for a superscript '²' but int() rejects it — parse must
        # not raise on odd input; the row is simply kept (treated as unknown time).
        out = winterm.parse_instances(
            "40996\t34540\t1000\tclaude.exe\n30880\t40996\t²\tclaude.exe\n")
        self.assertEqual(out, [("claude", "40996"), ("claude", "30880")])

    def test_legacy_three_column_without_ctime_not_folded(self):
        # a 3-column legacy row has no creation time -> never folded.
        out = winterm.parse_instances(
            "40996\t34540\tclaude.exe\n30880\t40996\tclaude.exe\n")
        self.assertEqual(out, [("claude", "40996"), ("claude", "30880")])

    def test_non_numeric_ppid_keeps_row(self):
        # a garbage ppid can't be in the matched set -> the row is kept.
        out = winterm.parse_instances("123\tNOPE\t1000\tclaude.exe\n")
        self.assertEqual(out, [("claude", "123")])

    def test_mixed_legacy_and_new_format(self):
        # a 2-col legacy line alongside a 4-col line: both parsed, neither folded
        # (the 2-col has no ppid; the 4-col's parent isn't matched).
        out = winterm.parse_instances(
            "111\tclaude.exe\n222\t999\t1000\tclaude.exe\n")
        self.assertEqual(out, [("claude", "111"), ("claude", "222")])


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
        # anchored to the scoped install path (@anthropic-ai/claude-code), not a
        # bare "claude-code" substring that an `npm install claude-code` line would
        # also hit. [\/] matches either path separator.
        self.assertIn(r"@anthropic-ai[\\/]claude-code", s)
        self.assertNotIn("-match 'claude-code'", s)
        # ParentProcessId + CreationDate are emitted so the launcher/worker pair can
        # be folded (one row per session) under PID recycling in parse_instances.
        self.assertIn("ParentProcessId", s)
        self.assertIn("CreationDate", s)
        # the query narrows to the two image names server-side (WQL -Filter) so each
        # poll marshals a handful of processes, not the whole table.
        self.assertIn("-Filter", s)
        self.assertIn("Name='claude.exe'", s)

    def test_list_with_injected_runner(self):
        out = winterm.list_claude_instances(
            run=lambda t: "22108\tclaude.exe\n35552\tclaude.exe\n")
        self.assertEqual(out, [("claude", "22108"), ("claude", "35552")])

    def test_list_folds_launcher_worker_pair(self):
        # the lister now emits ppid + ctime; list_claude_instances returns one row
        # per session (the worker child of an older launcher is folded away).
        out = winterm.list_claude_instances(
            run=lambda t: "40996\t34540\t1000\tclaude.exe\n30880\t40996\t2000\tclaude.exe\n")
        self.assertEqual(out, [("claude", "40996")])

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


class TestWindowTitles(unittest.TestCase):
    def test_parse_strips_blanks_and_dedups(self):
        out = winterm.parse_window_titles("  Term A \nChrome\nTerm A\n\n")
        self.assertEqual(out, ["Term A", "Chrome"])

    def test_build_script_reads_mainwindowtitle(self):
        s = winterm.build_window_titles_script()
        self.assertIn("Get-Process", s)
        self.assertIn("MainWindowTitle", s)

    def test_list_with_injected_runner(self):
        out = winterm.list_window_titles(run=lambda t: "Term A\nChrome\n")
        self.assertEqual(out, ["Term A", "Chrome"])

    def test_match_is_prefix_and_case_insensitive(self):
        self.assertTrue(winterm.window_match("windows terminal", ["Windows Terminal - claude"]))
        self.assertTrue(winterm.window_match("WT", ["wt: a job"]))

    def test_match_is_prefix_not_substring(self):
        # AppActivate matches the START of a title, not a mid-string occurrence.
        self.assertFalse(winterm.window_match("Terminal", ["Windows Terminal"]))

    def test_match_empty_target_is_false(self):
        self.assertFalse(winterm.window_match("", ["anything"]))
        self.assertFalse(winterm.window_match("  ", ["anything"]))

    def test_match_real_wt_tab_title_fails(self):
        # the exact bug: Windows Terminal's window title is the active TAB's name,
        # never the literal "Windows Terminal", so the default target finds nothing.
        self.assertFalse(winterm.window_match("Windows Terminal", ["⠂ Debug GUI app", "Chrome"]))

    def test_run_window_titles_nonzero_raises(self):
        fail = subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch("claude_continue.winterm._powershell_bin", return_value="powershell"), \
             mock.patch("claude_continue.winterm.subprocess.run", return_value=fail):
            with self.assertRaises(RuntimeError):
                winterm._run_window_titles(30.0)


class TestUtf16Units(unittest.TestCase):
    # a console UnicodeChar is a single UTF-16 code unit, so non-BMP text must be
    # split into surrogate halves or assigning it to a WCHAR raises TypeError.
    def test_bmp_text_is_one_unit_per_char(self):
        self.assertEqual(winterm._utf16_units("continue\r"), list("continue\r"))

    def test_non_bmp_char_splits_into_two_surrogate_units(self):
        units = winterm._utf16_units("a😀b")  # emoji is non-BMP -> 2 code units
        self.assertEqual(len(units), 4)        # a, hi-surrogate, lo-surrogate, b
        self.assertTrue(all(len(u) == 1 for u in units))  # each a valid 1-char WCHAR
        self.assertEqual(units[0], "a")
        self.assertEqual(units[-1], "b")


class TestContinueInstances(unittest.TestCase):
    # console-input injection: continue EVERY running Claude session by PID, no
    # tabs/panes/focus. _inject_one is Windows-ctypes (not unit-tested off-Windows);
    # the orchestration is tested with injected inject/is_alive/instances/list_fn.
    _ALIVE = staticmethod(lambda pid: True)

    def test_injects_continue_plus_enter_into_each_pid(self):
        calls = []
        out = winterm.continue_instances(
            "continue",
            instances=[("claude", "22108"), ("claude", "35552")],
            inject=lambda pid, keys: calls.append((pid, keys)), is_alive=self._ALIVE,
        )
        self.assertEqual(calls, [("22108", "continue\r"), ("35552", "continue\r")])
        self.assertEqual(len(out), 2)
        self.assertIn("22108", out[0])

    def test_dry_run_lists_without_injecting(self):
        calls = []
        out = winterm.continue_instances(
            "continue", instances=[("claude", "1")],
            inject=lambda pid, keys: calls.append(pid), dry_run=True)
        self.assertEqual(calls, [])
        self.assertEqual(len(out), 1)

    def test_uses_list_fn_when_instances_not_given(self):
        out = winterm.continue_instances(
            "continue", list_fn=lambda timeout: [("claude", "7")],
            inject=lambda pid, keys: None, is_alive=self._ALIVE)
        self.assertEqual(len(out), 1)
        self.assertIn("pid 7", out[0])

    def test_dead_pid_skipped_without_injecting(self):
        # a session that exited between listing and now -> skipped quietly (the
        # pid_alive recheck narrows the TOCTOU window before AttachConsole)
        injected = []
        out = winterm.continue_instances(
            "continue", instances=[("claude", "1"), ("claude", "2")],
            inject=lambda pid, keys: injected.append(pid),
            is_alive=lambda pid: pid == 2)  # is_alive sees int(pid); pid 1 has exited
        self.assertEqual(injected, ["2"])   # inject receives the original pid value
        self.assertEqual(len(out), 1)
        self.assertIn("pid 2", out[0])

    def test_partial_failure_returns_successes_and_surfaces_warning(self):
        # inject raising (e.g. attach denied) is isolated; the others still resume AND
        # the failure is surfaced (logged), not silently dropped — else a paused session
        # is left with no signal to retry.
        def inject(pid, keys):
            if pid == "1":
                raise RuntimeError("attach failed")
        with self.assertLogs("claude-continue", level="WARNING") as cm:
            out = winterm.continue_instances(
                "continue", instances=[("claude", "1"), ("claude", "2")], inject=inject, is_alive=self._ALIVE)
        self.assertEqual(len(out), 1)
        self.assertIn("pid 2", out[0])
        self.assertTrue(any("failed to resume" in m and "1 of 2" in m for m in cm.output))

    def test_total_failure_raises(self):
        def inject(pid, keys):
            raise RuntimeError("attach failed")
        with self.assertRaises(RuntimeError):
            winterm.continue_instances(
                "continue", instances=[("claude", "1")], inject=inject, is_alive=self._ALIVE)

    def test_no_instances_returns_empty(self):
        self.assertEqual(winterm.continue_instances("continue", instances=[]), [])


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


class TestSubprocessStdinHardening(unittest.TestCase):
    """Every PowerShell call from the GUI must redirect stdin to DEVNULL. A windowed
    app's STDIN handle can be left invalid by the console-injection fire
    (AttachConsole/FreeConsole), and inheriting it makes CreateProcess fail with
    "[WinError 6] The handle is invalid" — silently breaking the instance/ccusage
    polls for the rest of the session."""

    def _captured_kwargs(self, call):
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch("claude_continue.winterm._powershell_bin", return_value="powershell"), \
             mock.patch("claude_continue.winterm.subprocess.run", fake_run):
            call()
        return seen

    def test_run_instances_redirects_stdin(self):
        self.assertEqual(self._captured_kwargs(lambda: winterm._run_instances(5)).get("stdin"),
                         subprocess.DEVNULL)

    def test_run_window_titles_redirects_stdin(self):
        self.assertEqual(self._captured_kwargs(lambda: winterm._run_window_titles(5)).get("stdin"),
                         subprocess.DEVNULL)

    def test_send_keystroke_redirects_stdin(self):
        self.assertEqual(
            self._captured_kwargs(lambda: winterm.send_keystroke("continue", window_title="WT")).get("stdin"),
            subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
