import shlex
import unittest

import _support  # noqa: F401

from claude_continue import tasksched


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


if __name__ == "__main__":
    unittest.main()
