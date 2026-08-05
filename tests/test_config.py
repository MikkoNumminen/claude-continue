import json
import os
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401

from claude_continue import config

from claude_continue.config import (
    DEFAULT_FILTER,
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
        self.assertEqual(cfg.reset_offset, 0)  # trust the estimate until corrected


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
        os.environ["CLAUDE_CONTINUE_SKIP_DIRS"] = "HRManager, D:\\koodaamista\\rag"
        cfg = resolve(config_path=Path("/nonexistent"))
        self.assertIs(cfg.skip_busy, False)
        self.assertEqual(cfg.retry_cap, 3)
        self.assertEqual(cfg.every_hours, 5.0)
        self.assertEqual(cfg.filter, ["a", "b", "c"])
        self.assertEqual(cfg.skip_dirs, ["HRManager", "D:\\koodaamista\\rag"])

    def test_reset_offset_is_int_and_may_be_negative(self):
        # reset_offset is an int field (coerced from env/CLI) and is NOT floored —
        # a negative correction (estimate runs late) is valid.
        os.environ["CLAUDE_CONTINUE_RESET_OFFSET"] = "-600"
        cfg = resolve(config_path=Path("/nonexistent"))
        self.assertEqual(cfg.reset_offset, -600)
        self.assertEqual(timing_issues(cfg), [])  # not a clamped timing value

    def test_file_list_field_string_becomes_one_entry(self):
        # a hand-edited `"skip_dirs": "D:\\proj"` must act as ONE entry, not
        # char-iterate; NOT comma-split (paths may contain commas — the env var
        # is the comma-separated form).
        path = self._write({"skip_dirs": "D:\\proj,x", "filter": "claude"})
        cfg = resolve(config_path=path)
        self.assertEqual(cfg.skip_dirs, ["D:\\proj,x"])
        self.assertEqual(cfg.filter, ["claude"])

    def test_file_list_field_unusable_shape_keeps_default(self):
        # `"skip_dirs": true` fed verbatim would crash every iteration site —
        # including inside the GUI's Tk refresh callback, freezing the app.
        path = self._write({"skip_dirs": True, "filter": 5})
        cfg = resolve(config_path=path)
        self.assertEqual(cfg.skip_dirs, [])
        self.assertEqual(cfg.filter, DEFAULT_FILTER)

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


class TestSaveSetting(unittest.TestCase):
    """The GUI could only ever READ config, so a mode chosen there was forgotten at
    every restart — silently reverting to the default. Fine for a fire time you
    retype anyway; not fine for a switch that decides which sessions get typed
    into."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sub" / "config.json"
        self.addCleanup(self._tmp.cleanup)

    def test_round_trips_through_resolve(self):
        self.assertTrue(config.save_setting("require_limit", False, config_path=self.path))
        self.assertFalse(config.resolve(config_path=self.path).require_limit)

    def test_creates_a_missing_config_dir(self):
        self.assertFalse(self.path.parent.exists())
        self.assertTrue(config.save_setting("require_limit", False, config_path=self.path))
        self.assertTrue(self.path.exists())

    def test_merges_instead_of_overwriting_hand_edited_keys(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"buffer": 120, "skip_dirs": ["HRManager"]}),
                             encoding="utf-8")
        config.save_setting("require_limit", False, config_path=self.path)
        cfg = config.resolve(config_path=self.path)
        self.assertEqual(cfg.buffer, 120)
        self.assertEqual(cfg.skip_dirs, ["HRManager"])
        self.assertFalse(cfg.require_limit)

    def test_leaves_no_temp_file_behind(self):
        config.save_setting("require_limit", True, config_path=self.path)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.json"])

    def test_unwritable_location_reports_failure_rather_than_raising(self):
        # a read-only config dir must not take the app down; the choice just lasts
        # for this session only, which the GUI says out loud
        bad = Path(self._tmp.name) / "config.json" / "nested.json"  # parent is a file
        Path(self._tmp.name, "config.json").write_text("{}", encoding="utf-8")
        self.assertFalse(config.save_setting("require_limit", False, config_path=bad))

    def test_an_unknown_setting_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            config.save_setting("not_a_real_field", 1, config_path=self.path)

    def test_a_corrupt_existing_file_is_replaced_not_appended_to(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertTrue(config.save_setting("require_limit", False, config_path=self.path))
        self.assertFalse(config.resolve(config_path=self.path).require_limit)

    def test_a_bom_does_not_silently_discard_the_whole_file(self):
        # The config file is documented as hand-editable, and on Windows the obvious
        # editors (Notepad, PowerShell Out-File, VS Code "UTF-8 with BOM") prepend
        # one. Read with the platform default those bytes break json.load and EVERY
        # setting reverts to its default with nothing said.
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'\xef\xbb\xbf{"require_limit": false, "buffer": 120}')
        cfg = config.resolve(config_path=self.path)
        self.assertFalse(cfg.require_limit)
        self.assertEqual(cfg.buffer, 120)

    def test_a_plain_utf8_file_still_reads(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"require_limit": false}')
        self.assertFalse(config.resolve(config_path=self.path).require_limit)

    def test_non_ascii_values_survive_a_round_trip(self):
        # skip_dirs can hold a path with non-ASCII characters; writing utf-8 and
        # reading the platform default would mangle it
        config.save_setting("skip_dirs", [r"D:\koodaamista\Ääni"], config_path=self.path)
        self.assertEqual(config.resolve(config_path=self.path).skip_dirs,
                         [r"D:\koodaamista\Ääni"])
