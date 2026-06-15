import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import tmux


# list-panes -F output: id \t session_name \t window_name \t pane_title
_PANES = "\n".join([
    "%1\twork\teditor\t✳ claude — repoA",
    "%2\twork\tshell\tclaude — repoB",
    "%3\tmisc\tlogs\ttail -f",
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
        self.sent = []       # (pane_id, payload)
        self.send_args = []  # full args list per send-keys call

    def __call__(self, args, *, timeout):
        if args[0] == "list-panes":
            return self.panes
        if args[0] == "capture-pane":
            return self.captures.get(args[-1], "")
        if args[0] == "send-keys":
            self.send_args.append(list(args))
            self.sent.append((args[2], args[-1]))  # ("-t" <id>, last arg = literal text or key)
            return ""
        return ""


class TestMatch(unittest.TestCase):
    def test_filter_matches_window_or_title_not_session(self):
        # matches on what Claude labels: the pane title or window name
        self.assertTrue(tmux._matches(["claude"], None, False, "work", "editor", "✳ claude — x"))
        self.assertTrue(tmux._matches(["claude"], None, False, "work", "claude-win", "shell"))
        self.assertFalse(tmux._matches(["claude"], None, False, "misc", "logs", "tail -f"))

    def test_session_name_alone_does_not_match(self):
        # regression: a tmux session named after the repo dir ("claude-continue")
        # must NOT pull in every pane (editor/shell/log) just by its session name
        self.assertFalse(tmux._matches(["claude"], None, False, "claude-continue", "editor", "vim"))

    def test_session_targets_session_name_only(self):
        self.assertTrue(tmux._matches([], "work", False, "work", "shell", "claude — repoB"))
        # not incidental text in another session's title/window
        self.assertFalse(tmux._matches([], "work", False, "misc", "shell", "claude — work"))

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
        panes = tmux._parse_panes("garbage-no-tabs\n%9\ts\tw\tclaude — z", ["claude"], None, False)
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

    def test_dash_leading_text_is_guarded_and_delivered(self):
        fake = _FakeTmux()
        with mock.patch.object(tmux, "_tmux", fake):
            tmux.broadcast("-resume", ["claude"], force=True)
        self.assertIn(("%1", "-resume"), fake.sent)  # delivered literally
        # ...because `--` ends option parsing right before the text
        text_call = next(a for a in fake.send_args if a[-1] == "-resume")
        self.assertEqual(text_call[-2], "--")


class TestListSessions(unittest.TestCase):
    def test_reports_working_and_idle(self):
        fake = _FakeTmux()
        with mock.patch.object(tmux, "_tmux", fake):
            sessions = tmux.list_sessions(["claude"])
        self.assertEqual(sessions, [("✳ claude — repoA", "idle"), ("claude — repoB", "working")])


class TestBusyHeuristic(unittest.TestCase):
    def test_marker_far_up_in_scrollback_reads_as_idle(self):
        # the marker is well above the footer region (old scrollback); the live
        # bottom is an idle prompt -> idle, so the session is eligible to resume
        old = "esc to interrupt was mentioned ages ago"
        filler = "\n".join("output line %d" % i for i in range(16))
        fake = _FakeTmux(captures={"%1": old + "\n" + filler + "\n│ > \n? for shortcuts", "%2": "idle"})
        with mock.patch.object(tmux, "_tmux", fake):
            sessions = tmux.list_sessions(["claude"])
        self.assertIn(("✳ claude — repoA", "idle"), sessions)

    def test_marker_above_input_box_reads_as_working(self):
        # regression: Claude's footer renders the spinner ABOVE the input box, so
        # the marker is ~5 lines up from the bottom — a 3-line tail would miss it
        footer = ("assistant said something\n"
                  "✶ Working… (esc to interrupt · 2.1k tokens)\n"
                  "\n╭───────────╮\n│ >         │\n╰───────────╯\n? for shortcuts")
        fake = _FakeTmux(captures={"%1": footer, "%2": "idle"})
        with mock.patch.object(tmux, "_tmux", fake):
            sessions = tmux.list_sessions(["claude"])
        self.assertIn(("✳ claude — repoA", "working"), sessions)


class _FlakyTmux(_FakeTmux):
    """Like _FakeTmux but raises TmuxError for any per-pane op on `bad_pane`
    (simulates a pane that vanished between enumeration and use)."""
    def __init__(self, bad_pane, **kw):
        super().__init__(**kw)
        self.bad_pane = bad_pane

    def __call__(self, args, *, timeout):
        if args[0] in ("capture-pane", "send-keys") and self.bad_pane in args:
            raise tmux.TmuxError("can't find pane: %s" % self.bad_pane)
        return super().__call__(args, timeout=timeout)


class TestResilience(unittest.TestCase):
    def test_broadcast_skips_vanished_pane_and_fires_rest(self):
        fake = _FlakyTmux("%2")
        with mock.patch.object(tmux, "_tmux", fake):
            fired = tmux.broadcast("continue", ["claude"])
        self.assertEqual(fired, ["✳ claude — repoA"])  # %2 vanished -> skipped, not aborted

    def test_list_sessions_drops_vanished_pane(self):
        fake = _FlakyTmux("%2")
        with mock.patch.object(tmux, "_tmux", fake):
            sessions = tmux.list_sessions(["claude"])
        self.assertEqual(sessions, [("✳ claude — repoA", "idle")])  # %2 dropped, no crash


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
