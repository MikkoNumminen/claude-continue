import os
import unittest
from datetime import timedelta
from unittest import mock

import _support  # noqa: F401
from _support import utc

from claude_continue import doctor, osenv
from claude_continue.ccusage import CcusageUnavailable
from claude_continue.config import Config
from claude_continue.doctor import FAIL, OK, WARN
from claude_continue.model import Block


def _block(end):
    return Block(id="b", start=end - timedelta(hours=5), end=end, actual_end=None,
                 is_active=True, is_gap=False)


class _ForcePlatform:
    def __init__(self, plat):
        self.plat = plat

    def __enter__(self):
        self._old = os.environ.get(osenv.PLATFORM_ENV)
        os.environ[osenv.PLATFORM_ENV] = self.plat
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(osenv.PLATFORM_ENV, None)
        else:
            os.environ[osenv.PLATFORM_ENV] = self._old


class TestCcusageCheck(unittest.TestCase):
    def test_active(self):
        c = doctor._check_ccusage(Config(), lambda t: _block(utc(2026, 1, 2, 13)), lambda: utc(2026, 1, 2, 12))
        self.assertEqual(c.status, OK)
        self.assertIn("resets", c.detail)

    def test_idle_warns(self):
        c = doctor._check_ccusage(Config(), lambda t: None, lambda: utc(2026, 1, 2, 12))
        self.assertEqual(c.status, WARN)

    def test_unavailable_fails(self):
        def boom(t):
            raise CcusageUnavailable("nope")
        self.assertEqual(doctor._check_ccusage(Config(), boom, lambda: utc(2026, 1, 2, 12)).status, FAIL)

    def test_skipped_when_fixed_schedule(self):
        def boom(t):
            raise CcusageUnavailable("nope")
        c = doctor._check_ccusage(Config(at="09:00"), boom, lambda: utc(2026, 1, 2, 12))
        self.assertEqual(c.status, OK)
        self.assertIn("not needed", c.detail)


class TestNodeCheck(unittest.TestCase):
    def test_missing_fails_in_auto_mode(self):
        self.assertEqual(doctor._check_node(Config(), which=lambda n: None).status, FAIL)

    def test_missing_warns_in_fixed_mode(self):
        self.assertEqual(doctor._check_node(Config(at="09:00"), which=lambda n: None).status, WARN)

    def test_macos_no_stable_dir(self):
        with _ForcePlatform("macos"), mock.patch("claude_continue.doctor.launchd_mod.stable_node_dir", return_value=None):
            c = doctor._check_node(Config(), which=lambda n: "/opt/node/bin/node" if n == "node" else None)
        self.assertEqual(c.status, OK)
        self.assertIn("/opt/node/bin", c.detail)

    def test_macos_with_stable_dir(self):
        with _ForcePlatform("macos"), mock.patch("claude_continue.doctor.launchd_mod.stable_node_dir", return_value="/opt/homebrew/bin"):
            c = doctor._check_node(Config(), which=lambda n: "/Users/x/.nvm/versions/node/v22/bin/node")
        self.assertEqual(c.status, OK)
        self.assertIn("stable node dir", c.detail)

    def test_macos_volatile_no_stable_warns(self):
        with _ForcePlatform("macos"), mock.patch("claude_continue.doctor.launchd_mod.stable_node_dir", return_value=None):
            c = doctor._check_node(Config(), which=lambda n: "/Users/x/.nvm/versions/node/v22/bin/node")
        self.assertEqual(c.status, WARN)
        self.assertIn("version-pinned", c.detail)

    def test_windows_just_reports_presence(self):
        with _ForcePlatform("windows"):
            c = doctor._check_node(Config(), which=lambda n: "C:\\node\\node.exe")
        self.assertEqual(c.status, OK)
        self.assertNotIn("launchd", c.detail)


class TestAgentCheck(unittest.TestCase):
    def test_running(self):
        self.assertEqual(doctor._check_agent(lambda: ("running", "up")).status, OK)

    def test_absent(self):
        self.assertEqual(doctor._check_agent(lambda: ("absent", "no")).status, WARN)

    def test_installed_not_running(self):
        self.assertEqual(doctor._check_agent(lambda: ("installed", "idle")).status, WARN)


class TestConfigCheck(unittest.TestCase):
    def test_invalid_at_fails(self):
        self.assertEqual(doctor._check_config(Config(at="99:99")).status, FAIL)

    def test_summary_every(self):
        c = doctor._check_config(Config(every_hours=5, anchor="06:00"))
        self.assertEqual(c.status, OK)
        self.assertIn("every 5h", c.detail)

    def test_summary_keystroke(self):
        c = doctor._check_config(Config(keystroke=True, window_title="WT"))
        self.assertIn("keystroke", c.detail)

    def test_nonpositive_timing_warns(self):
        c = doctor._check_config(Config(poll_interval=0))
        self.assertEqual(c.status, WARN)
        self.assertIn("poll_interval", c.detail)

    def test_poll_interval_ignored_in_fixed_schedule(self):
        # poll/verify/timeout aren't used by a fixed schedule, so don't warn.
        c = doctor._check_config(Config(poll_interval=0, every_hours=5))
        self.assertEqual(c.status, OK)

    def test_retry_interval_still_warns_in_fixed_schedule(self):
        # retry_interval DOES drive the failed-fire backoff even in fixed mode.
        c = doctor._check_config(Config(retry_interval=0, every_hours=5))
        self.assertEqual(c.status, WARN)
        self.assertIn("retry_interval", c.detail)


class TestActionCheck(unittest.TestCase):
    def _check(self, cfg, **kw):
        kw.setdefault("which", lambda n: "/bin/" + n)
        kw.setdefault("exists", lambda p: True)
        kw.setdefault("preview", lambda: ["s"])
        return doctor._check_action(cfg, **kw)

    def test_exec_found(self):
        c = self._check(Config(exec_cmd="claude -p go"))
        self.assertEqual(c.status, OK)
        self.assertIn("headless", c.detail)

    def test_exec_binary_missing_warns(self):
        c = self._check(Config(exec_cmd="nope -p go"), which=lambda n: None)
        self.assertEqual(c.status, WARN)
        self.assertIn("not found on PATH", c.detail)

    def test_exec_empty_fails(self):
        self.assertEqual(self._check(Config(exec_cmd="   ")).status, FAIL)

    def test_exec_unparseable_fails(self):
        self.assertEqual(self._check(Config(exec_cmd='a "b')).status, FAIL)

    def test_macos_iterm_missing_fails(self):
        with _ForcePlatform("macos"):
            c = self._check(Config(), exists=lambda p: False)
        self.assertEqual(c.status, FAIL)
        self.assertIn("iTerm2", c.detail)

    def test_macos_sessions_ok(self):
        with _ForcePlatform("macos"):
            c = self._check(Config(), exists=lambda p: True, preview=lambda: ["sess A"])
        self.assertEqual(c.status, OK)

    def test_macos_no_sessions_warns(self):
        with _ForcePlatform("macos"):
            c = self._check(Config(), exists=lambda p: True, preview=lambda: [])
        self.assertEqual(c.status, WARN)

    def test_macos_preview_actionerror_warns(self):
        from claude_continue.action import ActionError
        with _ForcePlatform("macos"):
            c = self._check(Config(), exists=lambda p: True, preview=lambda: (_ for _ in ()).throw(ActionError("boom")))
        self.assertEqual(c.status, WARN)
        self.assertIn("boom", c.detail)

    def test_windows_no_config_warns(self):
        with _ForcePlatform("windows"):
            c = self._check(Config())
        self.assertEqual(c.status, WARN)
        self.assertIn("no resume action", c.detail)

    def test_windows_keystroke_no_powershell_fails(self):
        with _ForcePlatform("windows"):
            c = self._check(Config(keystroke=True), which=lambda n: None)
        self.assertEqual(c.status, FAIL)
        self.assertIn("PowerShell", c.detail)

    def test_windows_keystroke_ok(self):
        with _ForcePlatform("windows"):
            c = self._check(Config(keystroke=True), which=lambda n: "powershell.exe",
                            preview=lambda: ["keystroke 'continue' -> window 'Windows Terminal'"])
        self.assertEqual(c.status, OK)


class TestRunChecks(unittest.TestCase):
    def test_all_ok_on_macos(self):
        with _ForcePlatform("macos"):
            checks = doctor.run_checks(
                Config(),
                which=lambda n: "/opt/node/bin/node",
                iterm_exists=lambda p: True,
                ccusage_probe=lambda t: _block(utc(2026, 1, 2, 13)),
                scheduler_describe=lambda: ("running", "up"),
                action_preview=lambda: ["s"],
                now=lambda: utc(2026, 1, 2, 12),
            )
        self.assertEqual(doctor.worst_status(checks), OK)
        self.assertEqual({c.name for c in checks}, {"python", "platform", "ccusage", "node", "agent", "config", "action"})

    def test_worst_status_precedence(self):
        Check = doctor.Check
        self.assertEqual(doctor.worst_status([Check("a", OK, ""), Check("b", WARN, ""), Check("c", FAIL, "")]), FAIL)
        self.assertEqual(doctor.worst_status([Check("a", OK, ""), Check("b", WARN, "")]), WARN)
        self.assertEqual(doctor.worst_status([Check("a", OK, "")]), OK)


class TestDoctorCommand(unittest.TestCase):
    def _run(self, checks):
        import io
        from contextlib import redirect_stdout
        from claude_continue import cli
        args = cli.build_parser().parse_args(["doctor"])
        with mock.patch("claude_continue.cli.doctor.run_checks", return_value=checks):
            with redirect_stdout(io.StringIO()):
                return cli.cmd_doctor(args)

    def test_exit_1_on_fail(self):
        self.assertEqual(self._run([doctor.Check("x", FAIL, "bad")]), 1)

    def test_exit_0_on_warn(self):
        self.assertEqual(self._run([doctor.Check("x", WARN, "meh")]), 0)

    def test_exit_0_on_all_ok(self):
        self.assertEqual(self._run([doctor.Check("x", OK, "good")]), 0)


if __name__ == "__main__":
    unittest.main()
