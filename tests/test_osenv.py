import os
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import osenv


class TestDetect(unittest.TestCase):
    def test_override_windows(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "windows"}):
            self.assertEqual(osenv.detect(), "windows")
            self.assertTrue(osenv.is_windows())
            self.assertTrue(osenv.uses_task_scheduler())
            self.assertFalse(osenv.uses_launchd())

    def test_override_wsl(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "wsl"}):
            self.assertTrue(osenv.is_wsl())
            self.assertTrue(osenv.uses_task_scheduler())

    def test_override_macos(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "macos"}):
            self.assertTrue(osenv.is_macos())
            self.assertTrue(osenv.uses_launchd())
            self.assertFalse(osenv.uses_task_scheduler())


class TestResolveArgv(unittest.TestCase):
    def test_resolves_on_path(self):
        with mock.patch("claude_continue.osenv.shutil.which", return_value="/usr/local/bin/npx"):
            self.assertEqual(osenv.resolve_argv(["npx", "x"]), ["/usr/local/bin/npx", "x"])

    def test_keeps_unfound(self):
        with mock.patch("claude_continue.osenv.shutil.which", return_value=None):
            self.assertEqual(osenv.resolve_argv(["foo", "x"]), ["foo", "x"])

    def test_windows_wraps_cmd_shim_with_call(self):
        with mock.patch("claude_continue.osenv.shutil.which", return_value="C:\\n\\npx.cmd"), \
             mock.patch.object(osenv.os, "name", "nt"):
            self.assertEqual(osenv.resolve_argv(["npx", "x"]), ["cmd", "/c", "call", "C:\\n\\npx.cmd", "x"])

    def test_empty(self):
        self.assertEqual(osenv.resolve_argv([]), [])


class TestSplitCommand(unittest.TestCase):
    def test_posix_strips_quotes(self):
        with mock.patch.object(osenv.os, "name", "posix"):
            self.assertEqual(osenv.split_command('claude -p "go now"'), ["claude", "-p", "go now"])

    def test_windows_keeps_backslashes_and_strips_quotes(self):
        with mock.patch.object(osenv.os, "name", "nt"):
            self.assertEqual(
                osenv.split_command(r'C:\tools\claude.exe -p "go now"'),
                ["C:\\tools\\claude.exe", "-p", "go now"],
            )


class TestDetachedKwargs(unittest.TestCase):
    def test_posix(self):
        with mock.patch.object(osenv.os, "name", "posix"):
            self.assertEqual(osenv.detached_popen_kwargs(), {"start_new_session": True})

    def test_windows(self):
        with mock.patch.object(osenv.os, "name", "nt"):
            self.assertIn("creationflags", osenv.detached_popen_kwargs())


class TestPidAlive(unittest.TestCase):
    def test_self_is_alive(self):
        self.assertTrue(osenv.pid_alive(os.getpid()))

    def test_unused_pid_is_dead(self):
        self.assertFalse(osenv.pid_alive(999999))


if __name__ == "__main__":
    unittest.main()
