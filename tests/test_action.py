import os
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import action, osenv
from claude_continue.action import ActionError, perform
from claude_continue.config import Config


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


class TestResumeDispatch(unittest.TestCase):
    def test_linux_keystroke_does_not_route_to_powershell(self):
        # keystroke is Windows/WSL only; on Linux it must raise, not call winterm
        with _ForcePlatform("linux"), mock.patch("claude_continue.action.winterm.send_keystroke") as sk:
            with self.assertRaises(ActionError):
                perform(Config(keystroke=True), dry_run=True)
        sk.assert_not_called()

    def test_windows_keystroke_routes_to_winterm(self):
        with _ForcePlatform("windows"):
            out = perform(Config(keystroke=True), dry_run=True)
        self.assertTrue(out and "keystroke" in out[0])

    def test_windows_no_config_raises(self):
        with _ForcePlatform("windows"):
            with self.assertRaises(ActionError):
                perform(Config(), dry_run=True)


class TestExec(unittest.TestCase):
    def test_dry_run_returns_label_without_spawning(self):
        with mock.patch("claude_continue.action.subprocess.Popen") as popen:
            out = perform(Config(exec_cmd="claude -p go"), dry_run=True)
        self.assertEqual(out, ["exec: claude -p go"])
        popen.assert_not_called()

    def test_real_spawns_detached(self):
        with mock.patch("claude_continue.action.subprocess.Popen") as popen:
            perform(Config(exec_cmd="claude -p go"), dry_run=False)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][-2:], ["-p", "go"])  # argv[0] resolved on PATH
        self.assertIn("claude", os.path.basename(args[0][0]))
        # detached: start_new_session (POSIX) or creationflags (Windows)
        self.assertTrue(kwargs.get("start_new_session") or kwargs.get("creationflags"))

    def test_empty_exec_raises(self):
        with self.assertRaises(ActionError):
            perform(Config(exec_cmd="   "), dry_run=False)

    def test_unbalanced_quote_raises(self):
        with self.assertRaises(ActionError):
            perform(Config(exec_cmd='claude -p "oops'), dry_run=True)

    def test_popen_oserror_becomes_actionerror(self):
        with mock.patch("claude_continue.action.subprocess.Popen", side_effect=OSError("no such file")):
            with self.assertRaises(ActionError):
                perform(Config(exec_cmd="nope go"), dry_run=False)


class TestBroadcastRouting(unittest.TestCase):
    def test_routes_to_iterm_with_expected_kwargs(self):
        with mock.patch("claude_continue.action.iterm.broadcast", return_value=["s"]) as bc:
            out = perform(Config(text="continue", session="Job"), dry_run=True)
        self.assertEqual(out, ["s"])
        _, kwargs = bc.call_args
        self.assertEqual(kwargs["session"], "Job")
        self.assertTrue(kwargs["dry_run"])

    def test_broadcast_runtimeerror_becomes_actionerror(self):
        with mock.patch("claude_continue.action.iterm.broadcast", side_effect=RuntimeError("iTerm2 not running")):
            with self.assertRaises(ActionError):
                perform(Config(), dry_run=False)


if __name__ == "__main__":
    unittest.main()
