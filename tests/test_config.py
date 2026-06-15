import json
import os
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401

from claude_continue import config
from claude_continue.config import (
    MIN_TIMING_SECONDS,
    Config,
    clamp_timing,
    resolve,
    timing_issues,
)


class TestDefaults(unittest.TestCase):
    def test_sane_defaults(self):
        cfg = resolve(config_path=Path("/nonexistent/config.json"))
        self.assertEqual(cfg.buffer, 90)
        self.assertTrue(cfg.skip_busy)
        self.assertEqual(cfg.text, "continue")
        self.assertEqual(cfg.filter, ["claude", "✳"])
        self.assertEqual(cfg.retry_cap, 30)
        self.assertEqual(cfg.retry_interval, 120)


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

    def test_blank_window_cmd_restored_to_default(self):
        # a blanked window_cmd would make quota mode try to run an empty command
        cfg = resolve({"window_cmd": "   "}, config_path=Path("/nonexistent/config.json"))
        self.assertEqual(cfg.window_cmd, Config.window_cmd)


class TestTimingClamp(unittest.TestCase):
    def test_sane_values_report_no_issues(self):
        self.assertEqual(timing_issues(Config()), [])

    def test_nonpositive_values_are_flagged(self):
        cfg = Config(poll_interval=0, retry_interval=-5)
        flagged = {name for name, _v, _f in timing_issues(cfg)}
        self.assertEqual(flagged, {"poll_interval", "retry_interval"})

    def test_clamp_floors_in_place_and_reports(self):
        cfg = Config(poll_interval=0, retry_interval=-5, verify_delay=0, timeout=0)
        adjusted = clamp_timing(cfg)
        self.assertEqual(cfg.poll_interval, MIN_TIMING_SECONDS)
        self.assertEqual(cfg.retry_interval, MIN_TIMING_SECONDS)
        self.assertEqual(cfg.verify_delay, MIN_TIMING_SECONDS)
        self.assertEqual(cfg.timeout, MIN_TIMING_SECONDS)
        self.assertEqual(len(adjusted), 4)

    def test_clamp_leaves_good_values_untouched(self):
        cfg = Config()
        before = (cfg.poll_interval, cfg.retry_interval, cfg.verify_delay, cfg.timeout)
        self.assertEqual(clamp_timing(cfg), [])
        self.assertEqual((cfg.poll_interval, cfg.retry_interval, cfg.verify_delay, cfg.timeout), before)

    def test_wrong_type_from_file_is_treated_as_invalid(self):
        # The config-file path does not coerce types, so a stringy interval could
        # slip in; clamp_timing must not crash on the comparison, and must fix it.
        cfg = Config(poll_interval="0")
        adjusted = clamp_timing(cfg)
        self.assertEqual(cfg.poll_interval, MIN_TIMING_SECONDS)
        self.assertEqual([name for name, _v, _f in adjusted], ["poll_interval"])


if __name__ == "__main__":
    unittest.main()
