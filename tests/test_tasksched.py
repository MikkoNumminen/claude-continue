import os
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import osenv, tasksched


class TestBuildRunCommand(unittest.TestCase):
    def test_native_windows(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "windows"}):
            tr = tasksched.build_run_command(["claude-continue"], ["--buffer", "120"], None)
        self.assertIn("watch", tr)
        self.assertIn("--buffer", tr)
        self.assertNotIn("wsl.exe", tr)

    def test_wsl_wraps_with_distro(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "wsl", "WSL_DISTRO_NAME": "Ubuntu"}):
            tr = tasksched.build_run_command(["claude-continue"], [], None)
        self.assertIn("wsl.exe", tr)
        self.assertIn("Ubuntu", tr)
        self.assertIn("watch", tr)

    def test_wsl_without_distro_omits_flag(self):
        with mock.patch.dict(os.environ, {osenv.PLATFORM_ENV: "wsl"}, clear=False):
            os.environ.pop("WSL_DISTRO_NAME", None)
            tr = tasksched.build_run_command(["claude-continue"], [], None)
        self.assertIn("wsl.exe", tr)
        self.assertNotIn("-d", tr)


if __name__ == "__main__":
    unittest.main()
