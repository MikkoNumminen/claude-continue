import os
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401

from claude_continue.lock import AlreadyRunning, PidLock


class TestPidLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "watch.pid"

    def tearDown(self):
        try:
            if self.path.exists():
                self.path.unlink()
            os.rmdir(self.dir)
        except OSError:
            pass

    def test_acquire_writes_pid_and_release_removes(self):
        lock = PidLock(self.path)
        lock.acquire()
        self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
        lock.release()
        self.assertFalse(self.path.exists())

    def test_refuses_when_other_process_alive(self):
        # the parent process is alive and (almost certainly) != us
        self.path.write_text(str(os.getppid()))
        with self.assertRaises(AlreadyRunning):
            PidLock(self.path).acquire()

    def test_reclaims_stale_pidfile(self):
        self.path.write_text("999999")  # not a live pid
        lock = PidLock(self.path)
        lock.acquire()  # should not raise
        self.assertEqual(self.path.read_text().strip(), str(os.getpid()))

    def test_reclaims_own_pid(self):
        self.path.write_text(str(os.getpid()))
        PidLock(self.path).acquire()  # no raise
        self.assertEqual(self.path.read_text().strip(), str(os.getpid()))

    def test_context_manager(self):
        with PidLock(self.path):
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
