import threading
import time
import unittest

import _support  # noqa: F401

from claude_continue.action import ActionError
from claude_continue.gui import WatchController
from claude_continue.lock import AlreadyRunning


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


if __name__ == "__main__":
    unittest.main()
