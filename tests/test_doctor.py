import unittest
from datetime import timedelta

import _support  # noqa: F401
from _support import utc

from claude_continue import doctor
from claude_continue.ccusage import CcusageUnavailable
from claude_continue.config import Config
from claude_continue.doctor import FAIL, OK, WARN
from claude_continue.model import Block


def _block(end):
    return Block(id="b", start=end - timedelta(hours=5), end=end, actual_end=None,
                 is_active=True, is_gap=False)


def _by_name(checks):
    return {c.name: c for c in checks}


class TestIndividualChecks(unittest.TestCase):
    def test_ccusage_active(self):
        now = utc(2026, 1, 2, 12)
        c = doctor._check_ccusage(Config(), lambda t: _block(utc(2026, 1, 2, 13)), lambda: now)
        self.assertEqual(c.status, OK)
        self.assertIn("resets", c.detail)

    def test_ccusage_idle_warns(self):
        c = doctor._check_ccusage(Config(), lambda t: None, lambda: utc(2026, 1, 2, 12))
        self.assertEqual(c.status, WARN)

    def test_ccusage_unavailable_fails(self):
        def boom(t):
            raise CcusageUnavailable("nope")
        c = doctor._check_ccusage(Config(), boom, lambda: utc(2026, 1, 2, 12))
        self.assertEqual(c.status, FAIL)

    def test_ccusage_skipped_when_fixed_schedule(self):
        def boom(t):
            raise CcusageUnavailable("nope")
        c = doctor._check_ccusage(Config(at="09:00"), boom, lambda: utc(2026, 1, 2, 12))
        self.assertEqual(c.status, OK)
        self.assertIn("not needed", c.detail)

    def test_node_found(self):
        c = doctor._check_node(Config(), which=lambda n: "/opt/node/bin/node" if n == "node" else None)
        self.assertEqual(c.status, OK)
        self.assertIn("/opt/node/bin", c.detail)

    def test_node_missing_fails_in_auto_mode(self):
        c = doctor._check_node(Config(), which=lambda n: None)
        self.assertEqual(c.status, FAIL)

    def test_node_missing_warns_in_fixed_mode(self):
        c = doctor._check_node(Config(at="09:00"), which=lambda n: None)
        self.assertEqual(c.status, WARN)

    def test_iterm_present(self):
        c = doctor._check_iterm(Config(), exists=lambda p: True)
        self.assertEqual(c.status, OK)

    def test_iterm_missing_fails(self):
        c = doctor._check_iterm(Config(), exists=lambda p: False)
        self.assertEqual(c.status, FAIL)

    def test_iterm_not_needed_in_exec_mode(self):
        c = doctor._check_iterm(Config(exec_cmd="claude -p x"), exists=lambda p: False)
        self.assertEqual(c.status, OK)

    def test_launchd_not_installed(self):
        c = doctor._check_launchd(lambda: "not loaded (whatever)")
        self.assertEqual(c.status, WARN)

    def test_launchd_running(self):
        c = doctor._check_launchd(lambda: "\tstate = running\n")
        self.assertEqual(c.status, OK)

    def test_launchd_installed_not_running(self):
        c = doctor._check_launchd(lambda: "\tstate = waiting\n")
        self.assertEqual(c.status, WARN)

    def test_config_invalid_at_fails(self):
        c = doctor._check_config(Config(at="99:99"))
        self.assertEqual(c.status, FAIL)

    def test_config_summary_ok(self):
        c = doctor._check_config(Config(every_hours=5, anchor="06:00"))
        self.assertEqual(c.status, OK)
        self.assertIn("every 5h", c.detail)

    def test_action_exec(self):
        c = doctor._check_action(Config(exec_cmd="claude -p go"), preview=lambda: [])
        self.assertEqual(c.status, OK)
        self.assertIn("would run", c.detail)

    def test_action_no_matches_warns(self):
        c = doctor._check_action(Config(), preview=lambda: [])
        self.assertEqual(c.status, WARN)

    def test_action_matches_ok(self):
        c = doctor._check_action(Config(), preview=lambda: ["sess A"])
        self.assertEqual(c.status, OK)

    def test_action_preview_error_warns(self):
        def boom():
            raise RuntimeError("osascript died")
        c = doctor._check_action(Config(), preview=boom)
        self.assertEqual(c.status, WARN)
        self.assertIn("could not query", c.detail)


class TestRunChecks(unittest.TestCase):
    def test_all_ok(self):
        checks = doctor.run_checks(
            Config(),
            which=lambda n: "/opt/node/bin/node",
            iterm_exists=lambda p: True,
            ccusage_probe=lambda t: _block(utc(2026, 1, 2, 13)),
            launchd_status=lambda: "state = running",
            action_preview=lambda: ["s"],
            now=lambda: utc(2026, 1, 2, 12),
        )
        self.assertEqual(doctor.worst_status(checks), OK)
        names = _by_name(checks)
        self.assertEqual(set(names), {"python", "ccusage", "node", "iterm2", "agent", "config", "targets"})

    def test_worst_status_precedence(self):
        from claude_continue.doctor import Check
        self.assertEqual(doctor.worst_status([Check("a", OK, ""), Check("b", WARN, ""), Check("c", FAIL, "")]), FAIL)
        self.assertEqual(doctor.worst_status([Check("a", OK, ""), Check("b", WARN, "")]), WARN)
        self.assertEqual(doctor.worst_status([Check("a", OK, "")]), OK)


class TestDoctorCommand(unittest.TestCase):
    def _run(self, checks):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
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
