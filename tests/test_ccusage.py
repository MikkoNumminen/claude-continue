import os
import tempfile
import unittest

import _support  # noqa: F401
from _support import FIXTURES

from claude_continue import ccusage
from claude_continue.ccusage import CcusageUnavailable, _command, get_active_block

ENV = "CLAUDE_CONTINUE_CCUSAGE_CMD"


class TestCommand(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(ENV, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self._saved

    def test_default_command(self):
        self.assertEqual(_command()[:2], ["npx", "ccusage"])
        self.assertIn("--offline", _command())

    def test_env_override_is_shlex_split(self):
        os.environ[ENV] = "cat some file.json"
        self.assertEqual(_command(), ["cat", "some", "file.json"])


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
        self._set("cat %s" % os.path.join(FIXTURES, "active.json"))
        block = get_active_block()
        self.assertIsNotNone(block)
        self.assertTrue(block.is_active)

    def test_idle_returns_none(self):
        self._set("cat %s" % os.path.join(FIXTURES, "idle.json"))
        self.assertIsNone(get_active_block())

    def test_nonzero_exit_raises(self):
        self._set("false")
        with self.assertRaises(CcusageUnavailable):
            get_active_block()

    def test_non_json_raises(self):
        self._set("echo notjson")
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
        self._set("cat %s" % path)
        with self.assertRaises(CcusageUnavailable):
            get_active_block()


if __name__ == "__main__":
    unittest.main()
