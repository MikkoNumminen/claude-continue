import os
import unittest
from datetime import timedelta
from unittest import mock

import _support  # noqa: F401
from _support import utc

from claude_continue import action, limits, osenv, winterm
from claude_continue.action import ActionError, NothingToResume, perform
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


class TestKeystrokeAll(unittest.TestCase):
    def test_windows_keystroke_all_injects_into_every_session(self):
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.continue_instances",
                        return_value=["continue -> claude (pid 22108)", "continue -> claude (pid 35552)"]) as cont:
            out = perform(Config(keystroke_all=True, require_limit=False), dry_run=False)
        self.assertEqual(len(out), 2)
        cont.assert_called_once()
        self.assertEqual(cont.call_args.args[0], "continue")  # cfg.text flows through
        self.assertFalse(cont.call_args.kwargs.get("dry_run"))

    def test_skip_dirs_flow_through_to_continue_all(self):
        # the config's "leave that terminal alone" list must reach the injection
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.continue_instances", return_value=[]) as cont:
            perform(Config(keystroke_all=True, skip_dirs=["HRManager"], require_limit=False), dry_run=False)
        self.assertEqual(cont.call_args.kwargs.get("skip_dirs"), ["HRManager"])

    def test_no_sessions_running_returns_empty_not_error(self):
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.continue_instances", return_value=[]):
            out = perform(Config(keystroke_all=True, require_limit=False), dry_run=True)
        self.assertEqual(out, [])

    def test_injection_failure_is_wrapped_as_actionerror(self):
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.continue_instances",
                        side_effect=RuntimeError("nothing could be attached")):
            with self.assertRaises(ActionError):
                perform(Config(keystroke_all=True, require_limit=False), dry_run=False)

    def test_keystroke_all_ignored_on_wsl_falls_back_to_single(self):
        # WSL's Claude is a Linux process; console injection can't see it — so
        # keystroke_all must NOT route to the Windows injection path there.
        with _ForcePlatform("wsl"), \
             mock.patch("claude_continue.action.winterm.send_keystroke", return_value=["x"]) as single, \
             mock.patch("claude_continue.action.winterm.continue_instances") as cont:
            perform(Config(keystroke_all=True, keystroke=True), dry_run=True)
        single.assert_called_once()
        cont.assert_not_called()


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
    # iTerm broadcast is macOS's resume path — force the platform so these run
    # the same everywhere (on Linux/Windows the default resume raises instead).
    def test_routes_to_iterm_with_expected_kwargs(self):
        with _ForcePlatform("macos"), \
             mock.patch("claude_continue.action.iterm.broadcast", return_value=["s"]) as bc:
            out = perform(Config(text="continue", session="Job"), dry_run=True)
        self.assertEqual(out, ["s"])
        _, kwargs = bc.call_args
        self.assertEqual(kwargs["session"], "Job")
        self.assertTrue(kwargs["dry_run"])

    def test_broadcast_runtimeerror_becomes_actionerror(self):
        with _ForcePlatform("macos"), \
             mock.patch("claude_continue.action.iterm.broadcast", side_effect=RuntimeError("iTerm2 not running")):
            with self.assertRaises(ActionError):
                perform(Config(require_limit=False), dry_run=False)


class TestQuotaWindow(unittest.TestCase):
    def test_start_window_runs_window_cmd_headless(self):
        out = perform(Config(start_window=True, window_cmd="claude -p hi"), dry_run=True)
        self.assertEqual(out, ["open window: claude -p hi"])

    def test_exec_takes_precedence_over_start_window(self):
        out = perform(Config(start_window=True, exec_cmd="claude -p go", window_cmd="x y"), dry_run=True)
        self.assertEqual(out, ["exec: claude -p go"])

    def test_start_window_real_spawns_detached(self):
        with mock.patch("claude_continue.action.subprocess.Popen") as popen:
            perform(Config(start_window=True, window_cmd="claude -p hi"), dry_run=False)
        popen.assert_called_once()


class TestTmuxRouting(unittest.TestCase):
    def test_tmux_takes_priority_over_iterm_on_macos(self):
        # cfg.tmux must route to tmux even on macOS (where iTerm is the default)
        with _ForcePlatform("macos"), \
             mock.patch("claude_continue.action.tmux.broadcast", return_value=["pane1"]) as bc, \
             mock.patch("claude_continue.action.iterm.broadcast") as ib:
            out = perform(Config(tmux=True, text="continue"), dry_run=True)
        self.assertEqual(out, ["pane1"])
        ib.assert_not_called()
        _, kwargs = bc.call_args
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(kwargs["busy_pattern"], "esc to interrupt")

    def test_tmux_works_on_linux(self):
        # the whole point: a Linux user (no iTerm) can resume via tmux
        with _ForcePlatform("linux"), \
             mock.patch("claude_continue.action.tmux.broadcast", return_value=["p"]):
            out = perform(Config(tmux=True), dry_run=True)
        self.assertEqual(out, ["p"])

    def test_tmux_error_becomes_actionerror(self):
        from claude_continue import tmux as tmux_mod
        with _ForcePlatform("linux"), \
             mock.patch("claude_continue.action.tmux.broadcast", side_effect=tmux_mod.TmuxError("no tmux")):
            with self.assertRaises(ActionError):
                perform(Config(tmux=True, require_limit=False), dry_run=False)


class TestLimitGate(unittest.TestCase):
    """The gate that stopped claude-continue typing into sessions nobody blocked.

    Before it existed, a fire went to every running Claude regardless of state. On
    2026-08-05 that put 62 `continue`s into two sessions that had finished their
    work and were not rate-limited at all.
    """

    NOW = utc(2026, 8, 5, 6)

    def _inst(self, name, pid, cwd):
        return winterm.Instance(name=name, pid=pid, cwd=cwd)

    def _states(self, mapping):
        return lambda cwd: mapping.get(cwd, limits.UNKNOWN)

    def _frozen(self):
        """Pin action's clock, so "has the reset passed?" is not asked of the wall."""
        return mock.patch("claude_continue.action._utc_now", return_value=self.NOW)

    def test_only_spent_limits_pass(self):
        ready = limits.LimitState(known=True, limited=True, kind="session",
                                  reset_at=self.NOW - timedelta(minutes=1))
        waiting = limits.LimitState(known=True, limited=True, kind="session",
                                    reset_at=self.NOW + timedelta(hours=1))
        instances = [self._inst("claude", "1", "D:\\a"), self._inst("claude", "2", "D:\\b"),
                     self._inst("claude", "3", "D:\\c")]
        passed, held = action.gate_instances(
            instances, now=self.NOW,
            state_fn=self._states({"D:\\a": ready, "D:\\b": waiting, "D:\\c": limits.NOT_LIMITED}))
        self.assertEqual([i[1] for i in passed], ["1"])
        self.assertEqual([i[0][1] for i in held], ["2", "3"])

    def test_a_session_that_merely_finished_is_never_touched(self):
        instances = [self._inst("claude", "9", "D:\\done")]
        passed, held = action.gate_instances(
            instances, now=self.NOW, state_fn=self._states({"D:\\done": limits.NOT_LIMITED}))
        self.assertEqual(passed, [])
        self.assertFalse(held[0][1].limited)

    def test_unreadable_state_is_held_not_guessed(self):
        instances = [self._inst("claude", "9", "D:\\mystery")]
        passed, _held = action.gate_instances(instances, now=self.NOW, state_fn=self._states({}))
        self.assertEqual(passed, [])

    def test_instance_with_no_cwd_is_held(self):
        instances = [winterm.Instance(name="claude", pid="9", cwd="")]
        called = []
        passed, _held = action.gate_instances(
            instances, now=self.NOW, state_fn=lambda cwd: called.append(cwd) or limits.UNKNOWN)
        self.assertEqual(passed, [])
        self.assertEqual(called, [])  # no cwd, nothing to look up

    def test_model_cap_is_held_because_continue_cannot_clear_it(self):
        cap = limits.LimitState(known=True, limited=True, kind="model")
        instances = [self._inst("claude", "9", "D:\\opus")]
        passed, _held = action.gate_instances(instances, now=self.NOW,
                                              state_fn=self._states({"D:\\opus": cap}))
        self.assertEqual(passed, [])

    def test_windows_fire_only_reaches_the_limited_session(self):
        ready = limits.LimitState(known=True, limited=True, kind="session",
                                  reset_at=self.NOW - timedelta(minutes=1))
        instances = [self._inst("claude", "1", "D:\\a"), self._inst("claude", "2", "D:\\b")]
        with self._frozen(), _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.list_claude_instances", return_value=instances), \
             mock.patch("claude_continue.action.limits.state_for_cwd",
                        side_effect=self._states({"D:\\a": ready, "D:\\b": limits.NOT_LIMITED})), \
             mock.patch("claude_continue.action.winterm.continue_instances", return_value=["x"]) as cont:
            perform(Config(keystroke_all=True), dry_run=False)
        self.assertEqual([i[1] for i in cont.call_args.kwargs["instances"]], ["1"])

    def test_nothing_limited_raises_nothing_to_resume_with_the_real_reset(self):
        soon = self.NOW + timedelta(minutes=42)
        waiting = limits.LimitState(known=True, limited=True, kind="session", reset_at=soon)
        with self._frozen(), _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.list_claude_instances",
                        return_value=[self._inst("claude", "1", "D:\\a")]), \
             mock.patch("claude_continue.action.limits.state_for_cwd", return_value=waiting), \
             mock.patch("claude_continue.action.winterm.continue_instances") as cont:
            with self.assertRaises(NothingToResume) as ctx:
                perform(Config(keystroke_all=True), dry_run=False)
        cont.assert_not_called()
        self.assertEqual(ctx.exception.retry_at, soon)

    def test_dry_run_previews_instead_of_refusing(self):
        # The GUI validates the action with dry_run before starting a watch; the gate
        # must not make that look like a broken configuration.
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.list_claude_instances",
                        return_value=[self._inst("claude", "1", "D:\\a")]), \
             mock.patch("claude_continue.action.limits.state_for_cwd", return_value=limits.NOT_LIMITED), \
             mock.patch("claude_continue.action.winterm.continue_instances", return_value=[]):
            self.assertEqual(perform(Config(keystroke_all=True), dry_run=True), [])

    def test_gate_off_restores_the_old_fire_at_everything_behaviour(self):
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.list_claude_instances") as listed, \
             mock.patch("claude_continue.action.winterm.continue_instances", return_value=["x"]) as cont:
            perform(Config(keystroke_all=True, require_limit=False), dry_run=False)
        listed.assert_not_called()  # no extra enumeration when the gate is off
        self.assertIsNone(cont.call_args.kwargs["instances"])

    def test_skipped_dirs_are_not_counted_as_sessions(self):
        ready = limits.LimitState(known=True, limited=True, kind="session",
                                  reset_at=self.NOW - timedelta(minutes=1))
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.list_claude_instances",
                        return_value=[self._inst("claude", "1", "D:\\koodaamista\\HRManager")]), \
             mock.patch("claude_continue.action.limits.state_for_cwd", return_value=ready):
            snap = action.snapshot(Config(keystroke_all=True, skip_dirs=["HRManager"]), self.NOW)
        self.assertEqual(snap.ready, 0)

    def test_snapshot_counts_each_bucket(self):
        ready = limits.LimitState(known=True, limited=True, kind="session",
                                  reset_at=self.NOW - timedelta(minutes=1))
        waiting = limits.LimitState(known=True, limited=True, kind="session",
                                    reset_at=self.NOW + timedelta(hours=2))
        states = {"D:\\a": ready, "D:\\b": waiting, "D:\\c": limits.NOT_LIMITED}
        with _ForcePlatform("windows"), \
             mock.patch("claude_continue.action.winterm.list_claude_instances",
                        return_value=[self._inst("claude", str(n), cwd)
                                      for n, cwd in enumerate(states)]), \
             mock.patch("claude_continue.action.limits.state_for_cwd",
                        side_effect=self._states(states)):
            snap = action.snapshot(Config(keystroke_all=True), self.NOW)
        self.assertEqual((snap.ready, snap.waiting, snap.idle), (1, 1, 1))
        self.assertEqual(snap.soonest, self.NOW + timedelta(hours=2))
        self.assertTrue(snap.known)


if __name__ == "__main__":
    unittest.main()
