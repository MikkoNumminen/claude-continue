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

    def test_windows_body_doubles_percent_and_caret_escapes_operators(self):
        # a config value with cmd metacharacters must not expand (%) or inject (&):
        # % is doubled, and operators are caret-escaped so cmd treats them literally.
        body = tasksched.wrapper_body(["cc.exe", "watch", "--exec", "echo %PATH% & del x"], wsl=False)
        self.assertIn("%%PATH%%", body)        # % doubled (no expansion)
        self.assertIn("^&", body)              # operator caret-escaped (no command split)
        self.assertNotIn(" & ", body)          # no bare operator survives

    def test_windows_body_caret_escapes_embedded_quote_plus_operator(self):
        # the bug a plain \" falls into: an embedded quote flips cmd's quote-state and
        # re-exposes the operator. Caret-escaping keeps & literal regardless.
        body = tasksched.wrapper_body(["cc.exe", 'a" & del x'], wsl=False)
        self.assertIn("^&", body)
        self.assertNotIn(" & ", body)


class TestCmdEscaping(unittest.TestCase):
    def test_argv_quote(self):
        self.assertEqual(tasksched._argv_quote("watch"), '"watch"')
        self.assertEqual(tasksched._argv_quote("a b"), '"a b"')
        self.assertEqual(tasksched._argv_quote('a"b'), '"a\\"b"')          # embedded quote -> \"
        self.assertEqual(tasksched._argv_quote("C:\\d\\"), '"C:\\d\\\\"')  # trailing \ doubled before "

    def test_cmd_arg_caret_escapes_and_doubles_percent(self):
        a = tasksched._cmd_arg("echo %PATH% & del x")
        self.assertIn("%%PATH%%", a)           # % doubled
        self.assertIn("^&", a)                 # operator caret-escaped
        self.assertNotIn(" & ", a)
        self.assertTrue(a.startswith('^"'))    # even the quotes are caret-escaped (no cmd quote-state)

    def test_cmd_command_real_quotes(self):
        self.assertEqual(tasksched._cmd_command(r"C:\Program Files\cc.exe"), '"C:\\Program Files\\cc.exe"')
        self.assertEqual(tasksched._cmd_command("a%b.exe"), '"a%%b.exe"')   # % doubled, real quotes


@unittest.skipUnless(os.name == "nt", "needs real cmd.exe (Windows)")
class TestWrapperRoundTrip(unittest.TestCase):
    def test_args_survive_cmd_intact(self):
        # the authoritative check: run the generated wrapper through REAL cmd.exe and
        # confirm every (adversarial) arg reaches the target exe's argv unchanged, with
        # no operator injection and no % expansion.
        import json
        import shutil
        import sys
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        dump = os.path.join(d, "dump.py")
        with open(dump, "w") as f:
            f.write("import sys, json\nprint(json.dumps(sys.argv[1:]))\n")
        tricky = [
            'claude -p "hi" & echo PWNED',   # embedded quote + operator (the injection case)
            "Windows Terminal",               # space
            "a%PATH%b",                       # percent (must stay literal)
            "a&b", "c|d", "e>f", "g<h", "i^j", "(k)",  # bare operators
            "C:\\dir\\",                      # trailing backslash
        ]
        inner = [sys.executable, dump] + tricky
        body = tasksched.wrapper_body(inner, wsl=False)
        cmd_path = os.path.join(d, "run.cmd")
        with open(cmd_path, "w", newline="") as f:
            f.write(body)
        proc = subprocess.run(["cmd", "/c", cmd_path], capture_output=True, text=True)
        # exactly one output line (dump.py's JSON) — an injected `& echo PWNED` would
        # add a second line. ("PWNED" itself appears inside the JSON as the arg's literal
        # value, which is correct: the arg was passed through, not executed.)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "extra output => a command was injected: %r" % proc.stdout)
        self.assertEqual(json.loads(lines[0]), tricky)    # every arg round-trips exactly, intact


class TestTrValue(unittest.TestCase):
    def test_windows_tr_is_quoted_path(self):
        # /tr is the wrapper path, quoted so a spaced install path parses correctly
        self.assertEqual(tasksched.tr_value("C:\\x\\run.cmd", wsl=False, distro=""), '"C:\\x\\run.cmd"')

    def test_windows_tr_quotes_spaced_path(self):
        self.assertEqual(tasksched.tr_value(r"C:\Users\First Last\run.cmd", wsl=False, distro=""),
                         r'"C:\Users\First Last\run.cmd"')

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
        self.assertEqual(tr, '"C:\\x\\run.cmd"')   # /tr is now quoted (spaced-path safe)
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
