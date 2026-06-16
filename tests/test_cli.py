import io
import sys
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import cli, doctor


class _Cp1252Stream:
    """A stdout-like stream on a legacy code page: writing a glyph it can't encode
    raises UnicodeEncodeError, exactly like a real Windows cp1252 console (the
    crash the doctor hit on ✓)."""

    encoding = "cp1252"

    def __init__(self):
        self.text = ""

    def write(self, s):
        s.encode("cp1252")  # raises on ✓ / ✳ — mirrors the console
        self.text += s
        return len(s)

    def flush(self):
        pass

# Commands under test print() and argparse writes usage to stderr; capture both so
# a passing run is silent (and can't be mistaken for failure output). The test
# runner grabbed the real streams before this ran, so failures still surface.
_streams = {}


def setUpModule():
    _streams["out"], _streams["err"] = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()


def tearDownModule():
    sys.stdout, sys.stderr = _streams["out"], _streams["err"]


def _non_none(d):
    return {k: v for k, v in d.items() if v is not None}


class TestForceUtf8Stdio(unittest.TestCase):
    def test_noop_off_windows(self):
        with mock.patch.object(cli.os, "name", "posix"):
            cli._force_utf8_stdio()  # early return, never raises

    def test_windows_swallows_streams_without_reconfigure(self):
        # best-effort: a stream lacking reconfigure() (e.g. a captured StringIO)
        # must be tolerated, not crash the CLI.
        with mock.patch.object(cli.os, "name", "nt"), \
             mock.patch.object(cli.sys, "stdout", object()), \
             mock.patch.object(cli.sys, "stderr", object()):
            cli._force_utf8_stdio()  # AttributeError swallowed

    def test_windows_reconfigures_to_utf8(self):
        calls = []

        class FakeStream:
            def reconfigure(self, **kw):
                calls.append(kw)

        with mock.patch.object(cli.os, "name", "nt"), \
             mock.patch.object(cli.sys, "stdout", FakeStream()), \
             mock.patch.object(cli.sys, "stderr", FakeStream()):
            cli._force_utf8_stdio()
        self.assertTrue(all(c.get("encoding") == "utf-8" for c in calls))
        self.assertEqual(len(calls), 2)  # stdout + stderr

    def test_windows_rebuilds_stream_without_reconfigure(self):
        # the frozen windowed exe's stream has no reconfigure() but does expose a
        # raw .buffer — rebuild a UTF-8 TextIOWrapper over it (the in-place
        # reconfigure alone wasn't enough, which is why the shipped doctor still
        # crashed). After this, the glyphs encode instead of raising.
        class NoReconfigure:
            encoding = "cp1252"

            def __init__(self, buffer):
                self.buffer = buffer

        stream = NoReconfigure(io.BytesIO())
        with mock.patch.object(cli.os, "name", "nt"), \
             mock.patch.object(cli.sys, "stdout", stream), \
             mock.patch.object(cli.sys, "stderr", stream):
            cli._force_utf8_stdio()
            self.assertEqual(cli.sys.stdout.encoding, "utf-8")
            self.assertEqual(cli.sys.stderr.encoding, "utf-8")


class TestDoctorOutputOnLegacyConsole(unittest.TestCase):
    """The reported crash: `doctor` printed ✓ to a cp1252 console and tracebacked."""

    def test_emit_survives_unencodable_glyph(self):
        stream = _Cp1252Stream()
        with mock.patch.object(cli.sys, "stdout", stream):
            cli._emit("✓ all good")  # must not raise
        self.assertIn("all good", stream.text)

    def test_symbols_fall_back_to_ascii_on_legacy_codepage(self):
        with mock.patch.object(cli.sys, "stdout", _Cp1252Stream()):
            syms = cli._doctor_symbols()
        self.assertEqual(syms[doctor.OK], "[ok]")
        self.assertEqual(syms[doctor.WARN], "[!]")
        self.assertEqual(syms[doctor.FAIL], "[X]")

    def test_symbols_use_glyphs_when_encodable(self):
        class Utf8Stream:
            encoding = "utf-8"

        with mock.patch.object(cli.sys, "stdout", Utf8Stream()):
            syms = cli._doctor_symbols()
        self.assertEqual(syms[doctor.OK], "✓")
        self.assertEqual(syms[doctor.WARN], "!")
        self.assertEqual(syms[doctor.FAIL], "✗")

    def test_cmd_doctor_does_not_crash_on_cp1252(self):
        # detail carries the ✳ from the default filter — the other glyph that
        # crashed alongside the ✓ status symbol.
        checks = [doctor.Check("config", doctor.OK, "filter ['claude', '✳']")]
        stream = _Cp1252Stream()
        args = cli.build_parser().parse_args(["doctor"])
        with mock.patch("claude_continue.cli.doctor.run_checks", return_value=checks), \
             mock.patch.object(cli.sys, "stdout", stream):
            rc = cli.cmd_doctor(args)
        self.assertEqual(rc, 0)
        self.assertIn("config", stream.text)  # it printed, instead of raising
        # and the ASCII status symbol was actually selected (not just _emit's
        # replace-backstop masking the ✓) — proves the fallback is wired into the
        # command, not only the _doctor_symbols() unit.
        self.assertIn("[ok]", stream.text)


class TestGuiCommand(unittest.TestCase):
    def test_clears_stale_update_before_launching_gui(self):
        # cmd_gui is the only caller of cleanup_stale_update — it must run it (to
        # reap a leftover <exe>.old from a prior self-update) BEFORE opening the GUI.
        from claude_continue import gui, update
        order = []
        with mock.patch.object(update, "cleanup_stale_update", side_effect=lambda: order.append("cleanup")), \
             mock.patch.object(gui, "run", side_effect=lambda: order.append("run")):
            cli.cmd_gui(cli.build_parser().parse_args(["gui"]))
        self.assertEqual(order, ["cleanup", "run"])


class TestUninstallApp(unittest.TestCase):
    def test_app_flag_routes_to_complete_removal(self):
        from claude_continue import selfremove
        args = cli.build_parser().parse_args(["uninstall", "--app"])
        summary = {"agent_removed": True, "deleted": [], "bundle": "/Applications/x.app",
                   "bundle_scheduled": True, "frozen": True}
        with mock.patch.object(selfremove, "remove", return_value=summary) as rm:
            rc = cli.cmd_uninstall(args)
        self.assertEqual(rc, 0)
        rm.assert_called_once()
        self.assertTrue(rm.call_args.kwargs.get("purge_config"))

    def test_plain_uninstall_does_not_self_remove(self):
        from claude_continue import scheduler, selfremove
        args = cli.build_parser().parse_args(["uninstall"])
        with mock.patch.object(scheduler, "uninstall", return_value=True), \
             mock.patch.object(selfremove, "remove") as rm:
            rc = cli.cmd_uninstall(args)
        self.assertEqual(rc, 0)
        rm.assert_not_called()


class TestStatusQuotaMode(unittest.TestCase):
    def test_status_shows_open_window_not_send_continue(self):
        import contextlib
        import io
        from claude_continue.ccusage import CcusageUnavailable
        args = cli.build_parser().parse_args(["status", "--start-window"])
        buf = io.StringIO()
        with mock.patch("claude_continue.cli.get_active_block", side_effect=CcusageUnavailable("x")), \
             contextlib.redirect_stdout(buf):
            cli.cmd_status(args)
        out = buf.getvalue()
        self.assertIn("open window (quota)", out)
        self.assertNotIn("send", out)  # not the resume/broadcast wording


class TestUpdateCommand(unittest.TestCase):
    def _run(self, info, apply=False):
        from claude_continue import update
        args = cli.build_parser().parse_args(["update"] + (["--apply"] if apply else []))
        with mock.patch.object(update, "check", return_value=info):
            return cli.cmd_update(args)

    def test_up_to_date_returns_zero(self):
        from claude_continue.update import UpdateInfo
        self.assertEqual(self._run(UpdateInfo("0.5.1", "v0.5.1", False, None, None)), 0)

    def test_error_returns_one(self):
        from claude_continue.update import UpdateInfo
        self.assertEqual(self._run(UpdateInfo("0.5.1", None, False, None, None, error="net")), 1)

    def test_newer_from_source_reports_without_applying(self):
        from claude_continue import update
        from claude_continue.update import UpdateInfo
        info = UpdateInfo("0.5.1", "v0.6.0", True, "a.zip", "https://x")
        args = cli.build_parser().parse_args(["update", "--apply"])
        with mock.patch.object(update, "check", return_value=info), \
             mock.patch.object(update, "is_frozen", return_value=False), \
             mock.patch.object(update, "apply_update") as ap:
            rc = cli.cmd_update(args)
        self.assertEqual(rc, 0)
        ap.assert_not_called()  # from source -> never auto-applies

    def test_newer_frozen_with_apply_calls_apply_update(self):
        from claude_continue import update
        from claude_continue.update import UpdateInfo
        info = UpdateInfo("0.5.1", "v0.6.0", True, "a.zip", "https://x")
        args = cli.build_parser().parse_args(["update", "--apply"])
        with mock.patch.object(update, "check", return_value=info), \
             mock.patch.object(update, "is_frozen", return_value=True), \
             mock.patch.object(update, "apply_update", return_value="/Applications/x.app") as ap:
            rc = cli.cmd_update(args)
        self.assertEqual(rc, 0)
        ap.assert_called_once()


class TestParser(unittest.TestCase):
    def test_each_subcommand_parses(self):
        p = cli.build_parser()
        for cmd in ["status", "watch", "once", "fire", "install", "uninstall"]:
            args = p.parse_args([cmd])
            self.assertTrue(hasattr(args, "func"))

    def test_no_subcommand_errors(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])

    def test_keystroke_all_help_describes_console_injection_not_tabs(self):
        # the mechanism is console-input injection, NOT tab cycling — the help must
        # not promise a focus-stealing tab walk that never happens.
        import argparse
        p = cli.build_parser()
        sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
        help_text = sub.choices["watch"].format_help()
        self.assertIn("console input", help_text)
        self.assertNotIn("cycles a terminal", help_text)


class TestOverridesRoundTrip(unittest.TestCase):
    def test_install_flags_reconstruct_to_watch(self):
        p = cli.build_parser()
        install_args = p.parse_args([
            "install",
            "--exec", "claude -p 'go' --permission-mode bypassPermissions",
            "--buffer", "120",
            "--no-skip-busy",
            "--filter", "claude,✳",
            "--all",
            "--every", "5",
            "--anchor", "06:00",
        ])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))

        # re-parse the reconstructed flags under `watch`
        watch_args = p.parse_args(["watch"] + argv)
        before = _non_none(cli.build_overrides(install_args))
        after = _non_none(cli.build_overrides(watch_args))
        self.assertEqual(before, after)

    def test_skip_busy_true_emits_flag(self):
        argv = cli.overrides_to_argv({"skip_busy": True})
        self.assertEqual(argv, ["--skip-busy"])

    def test_skip_busy_false_emits_negation(self):
        argv = cli.overrides_to_argv({"skip_busy": False})
        self.assertEqual(argv, ["--no-skip-busy"])

    def test_none_values_skipped(self):
        self.assertEqual(cli.overrides_to_argv({"buffer": None, "text": None}), [])

    def test_keystroke_flags_roundtrip(self):
        p = cli.build_parser()
        install_args = p.parse_args(["install", "--keystroke", "--window-title", "My Term"])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))
        self.assertIn("--keystroke", argv)
        self.assertIn("--window-title", argv)
        watch_args = p.parse_args(["watch"] + argv)
        self.assertTrue(watch_args.keystroke)
        self.assertEqual(watch_args.window_title, "My Term")

    def test_keystroke_all_flag_roundtrips(self):
        p = cli.build_parser()
        install_args = p.parse_args(["install", "--keystroke-all"])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))
        self.assertIn("--keystroke-all", argv)
        watch_args = p.parse_args(["watch"] + argv)
        self.assertTrue(watch_args.keystroke_all)

    def test_keystroke_all_true_emits_bare_flag(self):
        self.assertEqual(cli.overrides_to_argv({"keystroke_all": True}), ["--keystroke-all"])

    def test_launchd_only_fields_not_emitted(self):
        self.assertEqual(cli.overrides_to_argv({"node_path": "/x", "log_path": "/y"}), [])

    def test_tmux_flags_roundtrip(self):
        p = cli.build_parser()
        install_args = p.parse_args(["install", "--tmux", "--tmux-busy-pattern", "Working…"])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))
        self.assertIn("--tmux", argv)
        self.assertIn("--tmux-busy-pattern", argv)
        watch_args = p.parse_args(["watch"] + argv)
        self.assertTrue(watch_args.tmux)
        self.assertEqual(watch_args.tmux_busy_pattern, "Working…")

    def test_tmux_true_emits_bare_flag_not_value(self):
        argv = cli.overrides_to_argv({"tmux": True})
        self.assertEqual(argv, ["--tmux"])  # not ["--tmux", "True"]

    def test_start_window_flags_roundtrip(self):
        p = cli.build_parser()
        install_args = p.parse_args(["install", "--start-window", "--window-cmd", "claude -p hi"])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))
        self.assertIn("--start-window", argv)
        self.assertIn("--window-cmd", argv)
        watch_args = p.parse_args(["watch"] + argv)
        self.assertTrue(watch_args.start_window)
        self.assertEqual(watch_args.window_cmd, "claude -p hi")

    def test_start_window_true_emits_bare_flag(self):
        self.assertEqual(cli.overrides_to_argv({"start_window": True}), ["--start-window"])


class TestFireCommand(unittest.TestCase):
    def test_fire_dry_run_calls_perform_with_dry_run(self):
        p = cli.build_parser()
        args = p.parse_args(["fire", "--dry-run", "--session", "Job"])
        with mock.patch("claude_continue.cli.action.perform", return_value=["Job"]) as m:
            rc = cli.cmd_fire(args)
        self.assertEqual(rc, 0)
        _, kwargs = m.call_args
        self.assertTrue(kwargs.get("dry_run"))

    def test_fire_real_calls_perform(self):
        p = cli.build_parser()
        args = p.parse_args(["fire"])
        with mock.patch("claude_continue.cli.action.perform", return_value=[]) as m:
            cli.cmd_fire(args)
        _, kwargs = m.call_args
        self.assertFalse(kwargs.get("dry_run"))


if __name__ == "__main__":
    unittest.main()
