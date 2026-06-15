import threading
import time
import unittest

import _support  # noqa: F401

from claude_continue.action import ActionError
from claude_continue.config import Config
from claude_continue.gui import WatchController, format_sessions, update_decision, watch_explanation
from claude_continue.lock import AlreadyRunning
from claude_continue.update import UpdateInfo


def _blocking_runner(cfg, *, logger, stop, sleep, use_lock, **kw):
    while not stop():
        sleep(0.05)


def _wait(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class TestWatchController(unittest.TestCase):
    def test_start_then_stop(self):
        c = WatchController(runner=_blocking_runner)
        c.start(cfg=object())
        self.assertTrue(_wait(c.is_watching))
        c.stop(timeout=2)
        self.assertFalse(c.is_watching())

    def test_request_stop_is_nonblocking_and_reports_stopping(self):
        # a runner that ignores the stop Event (simulates an in-flight, uninterruptible
        # osascript fire) until released — request_stop must NOT block on it
        release = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            release.wait(3)  # blocks regardless of the stop Event

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(c.is_watching))

        t0 = time.time()
        c.request_stop()
        self.assertLess(time.time() - t0, 0.5, "request_stop must return immediately")
        self.assertTrue(c.is_stopping())   # requested, but worker still alive
        self.assertTrue(c.is_watching())

        release.set()  # let the "fire" finish
        self.assertTrue(_wait(lambda: not c.is_watching()))
        self.assertFalse(c.is_stopping())

    def test_double_start_is_noop(self):
        calls = []

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            calls.append(1)
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(c.is_watching))
        c.start(object())  # already running -> no-op
        time.sleep(0.15)
        c.stop(timeout=2)
        self.assertEqual(len(calls), 1)

    def test_already_running_surfaces_error(self):
        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            raise AlreadyRunning(4321)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(lambda: not c.is_watching() and c.error))
        self.assertIn("4321", c.error)

    def test_action_error_surfaces(self):
        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            raise ActionError("no resume action")

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(lambda: c.error is not None))
        self.assertIn("no resume action", c.error)

    def test_fire_tap_counts_only_real_fires(self):
        # the tap runs synchronously inside logger.info, so once `logged` is set
        # all three records have been processed — deterministic, not timing-based
        logged = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            logger.info("armed: fire at 19:00")     # not a fire
            logger.info("fired -> %s", ["sessA"])    # counts
            logger.info("fire failed: iTerm down")   # must NOT count
            logged.set()
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(logged.wait(3))
        self.assertEqual(c.fires, 1)
        self.assertIsNotNone(c.last_fired)
        c.stop(timeout=2)


class TestFormatSessions(unittest.TestCase):
    def test_none_shows_checking(self):
        self.assertIn("checking", format_sessions(None, "", watching=False, cfg=Config()))

    def test_none_shows_note(self):
        self.assertIn("not running", format_sessions(None, "iTerm2 not running?", watching=False, cfg=Config()))

    def test_empty(self):
        self.assertIn("none found", format_sessions([], "", watching=False, cfg=Config()))

    def test_not_watching_lists_status_without_affect(self):
        out = format_sessions([("A", "working"), ("B", "idle")], "", watching=False, cfg=Config())
        self.assertIn("working", out)
        self.assertIn("idle", out)
        self.assertIn("A", out)
        self.assertNotIn("will resume", out)
        self.assertNotIn("skipped", out)

    def test_watching_marks_idle_resume_and_busy_skipped(self):
        out = format_sessions([("A", "working"), ("B", "idle")], "", watching=True, cfg=Config())
        self.assertIn("will resume", out)       # idle session
        self.assertIn("skipped (busy)", out)    # working session, skip_busy default

    def test_watching_force_resumes_busy(self):
        out = format_sessions([("A", "working")], "", watching=True, cfg=Config(force=True))
        self.assertIn("will resume", out)
        self.assertNotIn("skipped", out)

    def test_watching_no_skip_busy_resumes_busy(self):
        out = format_sessions([("A", "working")], "", watching=True, cfg=Config(skip_busy=False))
        self.assertIn("will resume", out)

    def test_watching_exec_mode_is_headless(self):
        out = format_sessions([("A", "idle")], "", watching=True, cfg=Config(exec_cmd="claude -p go"))
        self.assertIn("headless", out)
        self.assertNotIn("will resume", out)

    def test_long_list_truncated(self):
        many = [("S%d" % i, "idle") for i in range(12)]
        out = format_sessions(many, "", watching=False, cfg=Config())
        self.assertIn("...and 4 more", out)  # 12 - 8


class TestWatchExplanation(unittest.TestCase):
    def test_default_explains_continue_to_idle_and_skip_busy(self):
        out = watch_explanation(Config())
        self.assertIn("window to reset", out)
        self.assertIn("continue", out)            # the default text it sends
        self.assertIn("idle Claude sessions", out)
        self.assertIn("Busy sessions are skipped", out)

    def test_exec_mode_describes_headless_command(self):
        out = watch_explanation(Config(exec_cmd="claude -p go"))
        self.assertIn("claude -p go", out)
        self.assertIn("headlessly", out)
        self.assertNotIn("Busy sessions", out)    # no iTerm broadcast in exec mode

    def test_session_targeted(self):
        out = watch_explanation(Config(session="work"))
        self.assertIn("work", out)
        self.assertIn("session", out)

    def test_force_includes_busy(self):
        out = watch_explanation(Config(force=True))
        self.assertIn("force is on", out)

    def test_skip_busy_off(self):
        out = watch_explanation(Config(skip_busy=False))
        self.assertIn("skip-busy is off", out)


class TestUpdateDecision(unittest.TestCase):
    def test_none_info(self):
        kind, _ = update_decision(None, frozen=True)
        self.assertEqual(kind, "none")

    def test_error_surfaced(self):
        info = UpdateInfo("0.3.0", None, False, None, None, error="boom")
        kind, msg = update_decision(info, frozen=True)
        self.assertEqual(kind, "none")
        self.assertIn("boom", msg)

    def test_up_to_date(self):
        info = UpdateInfo("0.3.0", "v0.3.0", False, None, None)
        kind, msg = update_decision(info, frozen=True)
        self.assertEqual(kind, "none")
        self.assertIn("up to date", msg)

    def test_source_install_points_to_git(self):
        info = UpdateInfo("0.3.0", "v0.4.0", True, "a.zip", "https://x")
        kind, msg = update_decision(info, frozen=False)
        self.assertEqual(kind, "none")
        self.assertIn("git pull", msg)

    def test_no_asset_for_platform(self):
        info = UpdateInfo("0.3.0", "v0.4.0", True, None, None)
        kind, msg = update_decision(info, frozen=True)
        self.assertEqual(kind, "none")
        self.assertIn("no build", msg)

    def test_prompts_when_upgradable(self):
        info = UpdateInfo("0.3.0", "v0.4.0", True, "a.zip", "https://x")
        kind, _ = update_decision(info, frozen=True)
        self.assertEqual(kind, "prompt")


if __name__ == "__main__":
    unittest.main()
