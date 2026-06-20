import os
import shutil
import tempfile
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import selfremove


class TestSelfDeleteScripts(unittest.TestCase):
    def test_macos_script_waits_then_rms_and_self_deletes(self):
        s = selfremove.macos_self_delete_script("/Applications/claude-continue.app", 1234)
        self.assertIn("kill -0 1234", s)                       # wait for our process
        self.assertIn("rm -rf /Applications/claude-continue.app", s)
        self.assertIn('rm -f "$0"', s)                         # delete the helper itself

    def test_macos_script_quotes_spacey_path(self):
        s = selfremove.macos_self_delete_script("/Applications/My App.app", 1)
        self.assertIn("'/Applications/My App.app'", s)         # shlex.quote

    def test_windows_script_polls_pid_then_dels(self):
        s = selfremove.windows_self_delete_script(r"C:\a\cc.exe", pid=4321)
        self.assertIn('tasklist /FI "PID eq 4321"', s)   # poll our PID (capped loop)
        self.assertIn("goto ccwait", s)
        self.assertIn("waitfor /t 1 ", s)                # per-iteration delay
        self.assertIn("timeout /t 1 /nobreak", s)        # waitfor fallback
        self.assertNotIn("ping", s)
        self.assertIn(r'del /F /Q "C:\a\cc.exe"', s)
        self.assertIn('del "%~f0"', s)

    def test_windows_script_retries_delete(self):
        # the exe stays locked until the bootstrap+child exit -> a 2nd attempt
        s = selfremove.windows_self_delete_script(r"C:\a\cc.exe", pid=1, wait_s=4)
        self.assertIn("if %_i% GEQ 4 ", s)               # wait_s is the poll cap
        self.assertEqual(s.count(r'del /F /Q "C:\a\cc.exe"'), 2)


class TestRemovalTarget(unittest.TestCase):
    def test_source_has_no_target(self):
        with mock.patch.object(selfremove.update, "is_frozen", return_value=False):
            self.assertIsNone(selfremove.removal_target())

    def test_frozen_macos_returns_bundle(self):
        with mock.patch.object(selfremove.update, "is_frozen", return_value=True), \
             mock.patch.object(selfremove.osenv, "is_macos", return_value=True), \
             mock.patch.object(selfremove.update, "macos_bundle_path",
                               return_value="/Applications/claude-continue.app"):
            self.assertEqual(selfremove.removal_target(), "/Applications/claude-continue.app")


class TestRemove(unittest.TestCase):
    # All paths are mocked to temp dirs — these never touch the real config/logs.
    def test_removes_agent_and_config_no_bundle_from_source(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfgdir = os.path.join(tmp, "cfg")
        os.makedirs(cfgdir)
        logfile = os.path.join(tmp, "cc.log")
        open(logfile, "w").close()
        with mock.patch.object(selfremove.scheduler, "uninstall", return_value=True) as un, \
             mock.patch.object(selfremove, "leftover_paths", return_value=[cfgdir, logfile]), \
             mock.patch.object(selfremove.update, "is_frozen", return_value=False), \
             mock.patch.object(selfremove, "_spawn_self_delete") as spawn:
            summary = selfremove.remove(purge_config=True)
        un.assert_called_once()
        self.assertTrue(summary["agent_removed"])
        self.assertFalse(os.path.exists(cfgdir))   # config dir deleted
        self.assertFalse(os.path.exists(logfile))  # log deleted
        self.assertIsNone(summary["bundle"])
        spawn.assert_not_called()                  # from source: nothing to self-delete

    def test_frozen_spawns_self_delete(self):
        with mock.patch.object(selfremove.scheduler, "uninstall", return_value=False), \
             mock.patch.object(selfremove, "leftover_paths", return_value=[]), \
             mock.patch.object(selfremove, "removal_target",
                               return_value="/Applications/claude-continue.app"), \
             mock.patch.object(selfremove, "_spawn_self_delete") as spawn:
            summary = selfremove.remove(purge_config=False)
        spawn.assert_called_once_with("/Applications/claude-continue.app")
        self.assertEqual(summary["bundle"], "/Applications/claude-continue.app")
        self.assertTrue(summary["bundle_scheduled"])   # helper launched -> scheduled

    def test_spawn_failure_marks_not_scheduled(self):
        # if the detached helper can't launch, bundle_scheduled stays False so the
        # caller can warn instead of falsely reporting a clean removal
        with mock.patch.object(selfremove.scheduler, "uninstall", return_value=True), \
             mock.patch.object(selfremove, "leftover_paths", return_value=[]), \
             mock.patch.object(selfremove, "removal_target",
                               return_value="/Applications/claude-continue.app"), \
             mock.patch.object(selfremove, "_spawn_self_delete", side_effect=OSError("read-only")):
            summary = selfremove.remove()              # must not raise
        self.assertEqual(summary["bundle"], "/Applications/claude-continue.app")
        self.assertFalse(summary["bundle_scheduled"])

    def test_purge_false_keeps_config(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfgdir = os.path.join(tmp, "cfg")
        os.makedirs(cfgdir)
        with mock.patch.object(selfremove.scheduler, "uninstall", return_value=True), \
             mock.patch.object(selfremove, "leftover_paths", return_value=[cfgdir]), \
             mock.patch.object(selfremove.update, "is_frozen", return_value=False), \
             mock.patch.object(selfremove, "_spawn_self_delete"):
            selfremove.remove(purge_config=False)
        self.assertTrue(os.path.exists(cfgdir))    # purge_config=False keeps it

    def test_agent_failure_does_not_raise(self):
        with mock.patch.object(selfremove.scheduler, "uninstall", side_effect=RuntimeError("boom")), \
             mock.patch.object(selfremove, "leftover_paths", return_value=[]), \
             mock.patch.object(selfremove.update, "is_frozen", return_value=False), \
             mock.patch.object(selfremove, "_spawn_self_delete"):
            summary = selfremove.remove()             # must not raise mid-teardown
        self.assertFalse(summary["agent_removed"])


if __name__ == "__main__":
    unittest.main()
