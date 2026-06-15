import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import tmux


# list-panes -F output: id \t session \t window \t pane_title \t current_command
_PANES = "\n".join([
    "%1\twork\teditor\t✳ claude — repoA\tnode",
    "%2\twork\tshell\tclaude — repoB\tnode",
    "%3\tmisc\tlogs\ttail -f\ttail",
])
_CAPTURES = {
    "%1": "│ > \n? for shortcuts",                 # idle, waiting for input
    "%2": "✶ Working… (esc to interrupt · 1.2k tokens)",  # mid-turn
    "%3": "some log output",
}


class _FakeTmux:
    """Stands in for tmux._tmux: serves canned list/capture output, records sends."""
    def __init__(self, panes=_PANES, captures=None):
        self.panes = panes
        self.captures = captures if captures is not None else _CAPTURES
        self.sent = []  # (pane_id, payload)

    def __call__(self, args, *, timeout):
        if args[0] == "list-panes":
            return self.panes
        if args[0] == "capture-pane":
            return self.captures.get(args[-1], "")
        if args[0] == "send-keys":
            pane_id = args[2]
            payload = args[4] if len(args) > 4 else args[3]  # ["-l", text] or ["Enter"]
            self.sent.append((pane_id, payload))
            return ""
        return ""


class TestMatch(unittest.TestCase):
    def test_filter_substring_matches_any_name(self):
        self.assertTrue(tmux._matches(["claude"], None, False, "work", "editor", "✳ claude — x"))
        self.assertFalse(tmux._matches(["claude"], None, False, "misc", "logs", "tail -f"))

    def test_session_targets_one(self):
        self.assertTrue(tmux._matches([], "repoB", False, "work", "shell", "claude — repoB"))
        self.assertFalse(tmux._matches([], "repoB", False, "work", "shell", "claude — repoA"))

    def test_all_sessions_matches_everything(self):
        self.assertTrue(tmux._matches([], None, True, "misc", "logs", "tail -f"))

    def test_empty_filter_matches_nothing(self):
        self.assertFalse(tmux._matches([], None, False, "work", "editor", "claude — x"))


class TestParse(unittest.TestCase):
    def test_parses_and_filters(self):
        panes = tmux._parse_panes(_PANES, ["claude"], None, False)
        self.assertEqual([p["id"] for p in panes], ["%1", "%2"])  # %3 filtered out
        self.assertEqual(panes[0]["title"], "✳ claude — repoA")

    def test_skips_malformed_lines(self):
        panes = tmux._parse_panes("garbage-no-tabs\n%9\ts\tw\tclaude — z\tnode", ["claude"], None, False)
        self.assertEqual([p["id"] for p in panes], ["%9"])


class TestBroadcast(unittest.TestCase):
    def test_skip_busy_sends_only_to_idle(self):
        fake = _FakeTmux()
        with mock.patch.object(tmux, "_tmux", fake):
            fired = tmux.broadcast("continue", ["claude"], skip_busy=True)
        self.assertEqual(fired, ["✳ claude — repoA"])           # %2 was busy -> skipped
        self.assertIn(("%1", "continue"), fake.sent)
        self.assertIn(("%1", "Enter"), fake.sent)
        self.assertFalse(any(pid == "%2" for pid, _ in fake.sent))

    def test_force_sends_to_busy_too(self):
        fake = _FakeTmux()
        with mock.patch.object(tmux, "_tmux", fake):
            fired = tmux.broadcast("continue", ["claude"], skip_busy=True, force=True)
        self.assertEqual(set(fired), {"✳ claude — repoA", "claude — repoB"})
        self.assertIn(("%2", "continue"), fake.sent)

    def test_dry_run_sends_nothing_but_lists_targets(self):
        fake = _FakeTmux()
        with mock.patch.object(tmux, "_tmux", fake):
            fired = tmux.broadcast("continue", ["claude"], skip_busy=True, dry_run=True)
        self.assertEqual(fired, ["✳ claude — repoA"])
        self.assertEqual(fake.sent, [])

    def test_literal_flag_used_for_text(self):
        fake = _FakeTmux()
        with mock.patch.object(tmux, "_tmux", fake):
            tmux.broadcast("continue", ["claude"], force=True)
        # text goes through `send-keys -l`, Enter is a separate key event
        self.assertIn(("%1", "continue"), fake.sent)
        self.assertIn(("%1", "Enter"), fake.sent)


class TestListSessions(unittest.TestCase):
    def test_reports_working_and_idle(self):
        fake = _FakeTmux()
        with mock.patch.object(tmux, "_tmux", fake):
            sessions = tmux.list_sessions(["claude"])
        self.assertEqual(sessions, [("✳ claude — repoA", "idle"), ("claude — repoB", "working")])


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class TestLowLevel(unittest.TestCase):
    def test_missing_tmux_raises(self):
        with mock.patch.object(tmux.shutil, "which", return_value=None):
            with self.assertRaises(tmux.TmuxError):
                tmux.list_panes(["claude"])

    def test_no_server_is_empty_not_error(self):
        with mock.patch.object(tmux.shutil, "which", return_value="/usr/bin/tmux"), \
             mock.patch.object(tmux.subprocess, "run",
                               return_value=_Proc(1, "", "no server running on /tmp/tmux-501/default")):
            self.assertEqual(tmux.list_panes(["claude"]), [])

    def test_real_error_raises(self):
        with mock.patch.object(tmux.shutil, "which", return_value="/usr/bin/tmux"), \
             mock.patch.object(tmux.subprocess, "run", return_value=_Proc(1, "", "usage: tmux ...")):
            with self.assertRaises(tmux.TmuxError):
                tmux.list_panes(["claude"])


if __name__ == "__main__":
    unittest.main()
