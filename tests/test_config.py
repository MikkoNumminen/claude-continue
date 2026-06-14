import json
import os
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401

from claude_continue import config
from claude_continue.config import Config, resolve


class TestDefaults(unittest.TestCase):
    def test_sane_defaults(self):
        cfg = resolve(config_path=Path("/nonexistent/config.json"))
        self.assertEqual(cfg.buffer, 90)
        self.assertTrue(cfg.skip_busy)
        self.assertEqual(cfg.text, "continue")
        self.assertEqual(cfg.filter, ["claude", "✳"])
        self.assertEqual(cfg.retry_cap, 6)


class TestPrecedence(unittest.TestCase):
    def setUp(self):
        # isolate env
        self._saved = {k: v for k, v in os.environ.items() if k.startswith("CLAUDE_CONTINUE_")}
        for k in self._saved:
            del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("CLAUDE_CONTINUE_"):
                del os.environ[k]
        os.environ.update(self._saved)

    def _write(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        self.addCleanup(os.unlink, path)
        return Path(path)

    def test_file_over_defaults(self):
        path = self._write({"buffer": 200, "text": "go"})
        cfg = resolve(config_path=path)
        self.assertEqual(cfg.buffer, 200)
        self.assertEqual(cfg.text, "go")

    def test_env_over_file(self):
        path = self._write({"buffer": 200})
        os.environ["CLAUDE_CONTINUE_BUFFER"] = "300"
        cfg = resolve(config_path=path)
        self.assertEqual(cfg.buffer, 300)

    def test_overrides_over_env(self):
        os.environ["CLAUDE_CONTINUE_BUFFER"] = "300"
        cfg = resolve({"buffer": 400}, config_path=Path("/nonexistent"))
        self.assertEqual(cfg.buffer, 400)

    def test_none_override_ignored(self):
        os.environ["CLAUDE_CONTINUE_BUFFER"] = "300"
        cfg = resolve({"buffer": None}, config_path=Path("/nonexistent"))
        self.assertEqual(cfg.buffer, 300)

    def test_env_coercion_types(self):
        os.environ["CLAUDE_CONTINUE_SKIP_BUSY"] = "false"
        os.environ["CLAUDE_CONTINUE_RETRY_CAP"] = "3"
        os.environ["CLAUDE_CONTINUE_EVERY_HOURS"] = "5.0"
        os.environ["CLAUDE_CONTINUE_FILTER"] = "a, b ,c"
        cfg = resolve(config_path=Path("/nonexistent"))
        self.assertIs(cfg.skip_busy, False)
        self.assertEqual(cfg.retry_cap, 3)
        self.assertEqual(cfg.every_hours, 5.0)
        self.assertEqual(cfg.filter, ["a", "b", "c"])

    def test_bad_file_falls_back_to_defaults(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("{not json")
        self.addCleanup(os.unlink, path)
        cfg = resolve(config_path=Path(path))
        self.assertEqual(cfg.buffer, 90)  # default, no crash


if __name__ == "__main__":
    unittest.main()
