import os
import shlex
import subprocess
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import osenv, tasksched


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class TestWrapperBody(unittest.TestCase):
    def test_windows_body_is_cmd_script(self):
        inner = ["C:\\Program Files\\cc\\claude-continue.exe", "watch", "--exec", "claude -p 'go'"]
        body = tasksched.wrapper_body(inner, wsl=False)
        self.assertTrue(body.startswith("@echo off"))
        # the full command (with its spaces/quotes) lives inside the wrapper, not in /tr
        self.assertIn("claude-continue.exe", body)
        self.assertIn("--exec", body)

    def test_wsl_body_is_sh_script(self):
        inner = ["claude-continue", "watch", "--exec", "claude -p 'go'"]
        body = tasksched.wrapper_body(inner, wsl=True)
        self.assertTrue(body.startswith("#!/bin/sh"))
        self.assertIn("exec ", body)
        # the spaced/quoted arg is shell-quoted (escape style is shlex's own)
        self.assertIn(shlex.quote("claude -p 'go'"), body)


class TestTrValue(unittest.TestCase):
    def test_windows_tr_is_single_path(self):
        # /tr is just the wrapper path — no nested command-line quoting for schtasks
        self.assertEqual(tasksched.tr_value("C:\\x\\run.cmd", wsl=False, distro=""), "C:\\x\\run.cmd")

    def test_wsl_tr_invokes_wrapper_via_wsl(self):
        tr = tasksched.tr_value("/home/u/.config/claude-continue/run.sh", wsl=True, distro="Ubuntu")
        self.assertIn("wsl.exe", tr)
        self.assertIn("Ubuntu", tr)
        self.assertIn("run.sh", tr)
        self.assertIn("/bin/sh", tr)

    def test_wsl_tr_without_distro_omits_flag(self):
        tr = tasksched.tr_value("/home/u/run.sh", wsl=True, distro="")
        self.assertIn("wsl.exe", tr)
        self.assertNotIn("-d", tr)


class TestInstall(unittest.TestCase):
    def test_calls_schtasks_create(self):
        with mock.patch("claude_continue.tasksched._write_wrapper", return_value="C:\\x\\run.cmd"), \
             mock.patch("claude_continue.tasksched._run", return_value=_cp(0)) as run, \
             mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "windows"}):
            tr = tasksched.install(["cc"], ["--buffer", "120"], None)
        self.assertEqual(tr, "C:\\x\\run.cmd")
        argv = run.call_args[0][0]
        for token in ("/create", "/tn", tasksched.TASK_NAME, "/tr", "/sc", "onlogon"):
            self.assertIn(token, argv)

    def test_raises_on_failure(self):
        with mock.patch("claude_continue.tasksched._write_wrapper", return_value="x"), \
             mock.patch("claude_continue.tasksched._run", return_value=_cp(1, err="denied")):
            with self.assertRaises(RuntimeError):
                tasksched.install(["cc"], [], None)


class TestDescribe(unittest.TestCase):
    def test_absent_when_query_fails(self):
        with mock.patch("claude_continue.tasksched._run", return_value=_cp(1)):
            self.assertEqual(tasksched.describe()[0], "absent")

    def test_running(self):
        with mock.patch("claude_continue.tasksched._run", return_value=_cp(0, "TaskName: x\nStatus: Running\n")):
            self.assertEqual(tasksched.describe()[0], "running")

    def test_installed_when_ready(self):
        with mock.patch("claude_continue.tasksched._run", return_value=_cp(0, "Status: Ready\n")):
            self.assertEqual(tasksched.describe()[0], "installed")


class TestUninstall(unittest.TestCase):
    def test_deletes_task_and_removes_wrapper(self):
        wp = mock.Mock()
        with mock.patch("claude_continue.tasksched._run", return_value=_cp(0)) as run, \
             mock.patch("claude_continue.tasksched.wrapper_path", return_value=wp):
            self.assertTrue(tasksched.uninstall())
        self.assertIn("/delete", run.call_args[0][0])
        wp.unlink.assert_called_once()


if __name__ == "__main__":
    unittest.main()
