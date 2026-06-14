import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import action
from claude_continue.action import ActionError, perform
from claude_continue.config import Config


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
        self.assertEqual(args[0], ["claude", "-p", "go"])
        self.assertTrue(kwargs.get("start_new_session"))

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
