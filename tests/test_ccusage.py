import os
import sys
import tempfile
import unittest

import _support  # noqa: F401
from _support import FIXTURES

from claude_continue import ccusage  # noqa: F401
from claude_continue.ccusage import CcusageUnavailable, _command, get_active_block

ENV = "CLAUDE_CONTINUE_CCUSAGE_CMD"

# Cross-platform stand-ins for the canned-output shell commands (no cat/false/echo,
# which don't exist on Windows). Everything is quoted — split_command strips the
# quotes, so the -c code (which contains spaces) survives as a single argv token.
_DUMP = "import sys;sys.stdout.write(open(sys.argv[1]).read())"


def _dump_file(path):
    return '"%s" -c "%s" "%s"' % (sys.executable, _DUMP, path)


def _exit(code):
    return '"%s" -c "%s"' % (sys.executable, "import sys;sys.exit(%d)" % code)


class TestCommand(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self._saved

    def test_default_command(self):
        cmd = _command()  # on Windows, npx.cmd is wrapped as: cmd /c call <...npx.cmd>
        self.assertTrue(any("npx" in part for part in cmd), cmd)
        self.assertIn("ccusage", cmd)
        self.assertIn("--offline", cmd)

    def test_env_override_is_split(self):
        os.environ[ENV] = "cat some file.json"
        cmd = _command()
        self.assertEqual(cmd[-2:], ["some", "file.json"])  # split preserved
        self.assertIn("cat", os.path.basename(cmd[0]))


class TestGetActiveBlock(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self._saved

    def _set(self, cmd):
        os.environ[ENV] = cmd

    def test_success_returns_block(self):
        self._set(_dump_file(os.path.join(FIXTURES, "active.json")))
        block = get_active_block()
        self.assertIsNotNone(block)
        self.assertTrue(block.is_active)

    def test_idle_returns_none(self):
        self._set(_dump_file(os.path.join(FIXTURES, "idle.json")))
        self.assertIsNone(get_active_block())

    def test_nonzero_exit_raises(self):
        self._set(_exit(1))
        with self.assertRaises(CcusageUnavailable):
            get_active_block()

    def test_non_json_raises(self):
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("notjson")
        self.addCleanup(os.unlink, path)
        self._set(_dump_file(path))
        with self.assertRaises(CcusageUnavailable):
            get_active_block()

    def test_missing_binary_raises(self):
        self._set("this_binary_does_not_exist_zzz")
        with self.assertRaises(CcusageUnavailable):
            get_active_block()

    def test_unexpected_shape_raises_unavailable(self):
        # valid JSON, but a block missing required keys -> KeyError -> CcusageUnavailable
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write('{"blocks": [{"isGap": false, "isActive": true}]}')
        self.addCleanup(os.unlink, path)
        self._set(_dump_file(path))
        with self.assertRaises(CcusageUnavailable):
            get_active_block()

    def test_non_dict_payload_raises_unavailable(self):
        # ccusage emits a top-level array -> payload.get hits a list (AttributeError);
        # it must surface as CcusageUnavailable, not crash the daemon.
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("[]")
        self.addCleanup(os.unlink, path)
        self._set(_dump_file(path))
        with self.assertRaises(CcusageUnavailable):
            get_active_block()

    def test_numeric_timestamp_raises_unavailable(self):
        # a non-string timestamp -> parse_iso's .endswith hits an int (AttributeError);
        # also must surface as CcusageUnavailable.
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write('{"blocks": [{"id": "x", "startTime": 123, "endTime": 456, "isActive": true}]}')
        self.addCleanup(os.unlink, path)
        self._set(_dump_file(path))
        with self.assertRaises(CcusageUnavailable):
            get_active_block()

    def test_overflowing_timestamp_raises_unavailable(self):
        # a syntactically-valid ISO timestamp whose UTC conversion overflows datetime
        # (year 9999 with a negative offset) -> OverflowError, not ValueError; must
        # still surface as CcusageUnavailable, never crash the daemon.
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write('{"blocks": [{"id": "x", "startTime": "2024-01-01T00:00:00Z", '
                    '"endTime": "9999-12-31T23:59:59-14:00", "isActive": true}]}')
        self.addCleanup(os.unlink, path)
        self._set(_dump_file(path))
        with self.assertRaises(CcusageUnavailable):
            get_active_block()


class TestCcusageStdinHardening(unittest.TestCase):
    def test_get_active_block_redirects_stdin(self):
        # the GUI polls ccusage on a timer; it must not inherit the windowed app's
        # STDIN handle (left invalid by a console-injection fire -> WinError 6).
        import subprocess
        from unittest import mock
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess([], 0, "{}", "")

        with mock.patch("claude_continue.ccusage.subprocess.run", fake_run):
            try:
                get_active_block(timeout=5)
            except CcusageUnavailable:
                pass  # parsing the stub output may fail; we only assert the stdin kwarg
        self.assertEqual(seen.get("stdin"), subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
