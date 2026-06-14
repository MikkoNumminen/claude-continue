import os
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import osenv, scheduler
from claude_continue.config import Config


class TestInstallDispatch(unittest.TestCase):
    def test_macos_uses_launchd(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "macos"}), \
             mock.patch("claude_continue.scheduler.launchd.node_path_value", return_value="/p"), \
             mock.patch("claude_continue.scheduler.launchd.install", return_value="/x/plist") as li:
            lines = scheduler.install(["claude-continue"], ["--buffer", "120"], Config())
        program_args = li.call_args[0][0]
        self.assertEqual(program_args, ["claude-continue", "watch", "--buffer", "120"])
        self.assertTrue(any("launchd" in ln for ln in lines))

    def test_windows_uses_tasksched(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "windows"}), \
             mock.patch("claude_continue.scheduler.tasksched.install", return_value="cc watch") as ti:
            lines = scheduler.install(["cc"], [], Config())
        ti.assert_called_once()
        self.assertTrue(any("scheduled task" in ln for ln in lines))


class TestUninstallDispatch(unittest.TestCase):
    def test_windows(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "windows"}), \
             mock.patch("claude_continue.scheduler.tasksched.uninstall", return_value=True) as tu:
            self.assertTrue(scheduler.uninstall())
        tu.assert_called_once()


class TestLinuxUnsupported(unittest.TestCase):
    def test_install_raises(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "linux"}):
            with self.assertRaises(RuntimeError):
                scheduler.install(["claude-continue"], [], Config())

    def test_uninstall_returns_false(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "linux"}):
            self.assertFalse(scheduler.uninstall())

    def test_describe_absent(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "linux"}):
            self.assertEqual(scheduler.describe()[0], "absent")


class TestDescribe(unittest.TestCase):
    def test_launchd_running(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "macos"}), \
             mock.patch("claude_continue.scheduler.launchd.status", return_value="\tstate = running\n"):
            self.assertEqual(scheduler.describe()[0], "running")

    def test_launchd_absent(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "macos"}), \
             mock.patch("claude_continue.scheduler.launchd.status", return_value="not loaded (x)"):
            self.assertEqual(scheduler.describe()[0], "absent")

    def test_windows_delegates_to_tasksched(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "windows"}), \
             mock.patch("claude_continue.scheduler.tasksched.describe", return_value=("running", "x")):
            self.assertEqual(scheduler.describe(), ("running", "x"))


if __name__ == "__main__":
    unittest.main()
