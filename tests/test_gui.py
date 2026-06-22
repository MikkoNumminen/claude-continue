import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import _support  # noqa: F401

from claude_continue import osenv
from claude_continue.action import ActionError
from claude_continue.config import Config
from claude_continue.gui import (
    WatchController,
    effective_cfg,
    format_instances,
    format_reset_field,
    format_sessions,
    offset_from_clock,
    parse_reset_input,
    reset_controls_state,
    should_annotate_continue,
    should_auto_recheck,
    update_button_color,
    update_button_label,
    update_decision,
    watch_explanation,
    watching_note,
    win_instances_mode,
)
from claude_continue.gui import _BTN_UPDATE_AVAILABLE, _BTN_UP_TO_DATE, _pick_family
from claude_continue.lock import AlreadyRunning
from claude_continue.update import UpdateInfo


def _blocking_runner(cfg, *, logger, stop, sleep, use_lock, **kw):
    while not stop():
        sleep(0.05)


def _wait(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class _ForcePlatform:
    """Force osenv.detect() via the platform env var, restoring it on exit."""

    def __init__(self, plat):
        self.plat = plat

    def __enter__(self):
        self._old = os.environ.get(osenv.PLATFORM_ENV)
        os.environ[osenv.PLATFORM_ENV] = self.plat
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(osenv.PLATFORM_ENV, None)
        else:
            os.environ[osenv.PLATFORM_ENV] = self._old


class TestWatchController(unittest.TestCase):
    def test_start_then_stop(self):
        c = WatchController(runner=_blocking_runner)
        c.start(cfg=object())
        self.assertTrue(_wait(c.is_watching))
        c.stop(timeout=2)
        self.assertFalse(c.is_watching())

    def test_request_stop_is_nonblocking_and_reports_stopping(self):
        # a runner that ignores the stop Event (simulates an in-flight, uninterruptible
        # osascript fire) until released — request_stop must NOT block on it
        release = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            release.wait(3)  # blocks regardless of the stop Event

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(c.is_watching))

        t0 = time.time()
        c.request_stop()
        self.assertLess(time.time() - t0, 0.5, "request_stop must return immediately")
        self.assertTrue(c.is_stopping())   # requested, but worker still alive
        self.assertTrue(c.is_watching())

        release.set()  # let the "fire" finish
        self.assertTrue(_wait(lambda: not c.is_watching()))
        self.assertFalse(c.is_stopping())

    def test_double_start_is_noop(self):
        calls = []

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            calls.append(1)
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(c.is_watching))
        c.start(object())  # already running -> no-op
        time.sleep(0.15)
        c.stop(timeout=2)
        self.assertEqual(len(calls), 1)

    def test_already_running_surfaces_error(self):
        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            raise AlreadyRunning(4321)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(lambda: not c.is_watching() and c.error))
        self.assertIn("4321", c.error)

    def test_action_error_surfaces(self):
        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            raise ActionError("no resume action")

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(_wait(lambda: c.error is not None))
        self.assertIn("no resume action", c.error)

    def test_fire_tap_counts_only_real_fires(self):
        # the tap runs synchronously inside logger.info, so once `logged` is set
        # all three records have been processed — deterministic, not timing-based
        logged = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            logger.info("armed: fire at 19:00")     # not a fire
            logger.info("fired -> %s", ["sessA"])    # counts
            logger.info("fire failed: iTerm down")   # must NOT count
            logged.set()
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(logged.wait(3))
        self.assertEqual(c.fires, 1)
        self.assertIsNotNone(c.last_fired)
        c.stop(timeout=2)

    def test_warning_is_surfaced(self):
        # a failed fire logs at WARNING; the controller must capture it so the UI
        # can show it (previously these went nowhere and the failure was silent).
        logged = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            logger.warning("fire failed: window not found: Windows Terminal")
            logged.set()
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(logged.wait(3))
        self.assertTrue(_wait(lambda: c.last_warning is not None))
        self.assertIn("window not found", c.last_warning[1])
        c.stop(timeout=2)

    def test_real_fire_clears_a_prior_warning(self):
        done = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            logger.warning("fire failed: boom")     # sets last_warning
            logger.info("fired -> %s", ["sessA"])    # a real fire clears it
            done.set()
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(done.wait(3))
        self.assertIsNone(c.last_warning)
        self.assertEqual(c.fires, 1)
        c.stop(timeout=2)

    def test_info_records_do_not_set_warning(self):
        logged = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            logger.info("armed: fire at 19:00")  # INFO must not register as a warning
            logged.set()
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner)
        c.start(object())
        self.assertTrue(logged.wait(3))
        self.assertIsNone(c.last_warning)
        c.stop(timeout=2)

    def test_log_path_persists_watch_output(self):
        import os as _os
        import shutil
        import tempfile

        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # a non-existent parent dir must be created (best-effort)
        path = _os.path.join(d, "sub", "gui.log")
        done = threading.Event()

        def runner(cfg, *, logger, stop, sleep, use_lock, **kw):
            logger.info("watch started")
            logger.warning("fire failed: boom")
            done.set()
            while not stop():
                sleep(0.05)

        c = WatchController(runner=runner, log_path=path)
        # close the file handler on teardown so the rotating log isn't left open
        self.addCleanup(lambda: [h.close() for h in list(c._logger.handlers)])
        c.start(object())
        self.assertTrue(done.wait(3))
        c.stop(timeout=2)
        with open(path, encoding="utf-8") as f:
            contents = f.read()
        self.assertIn("watch started", contents)
        self.assertIn("fire failed: boom", contents)

    def test_default_gui_log_path_lives_beside_the_config(self):
        from claude_continue.config import CONFIG_PATH
        from claude_continue.gui import _default_gui_log_path
        self.assertEqual(_default_gui_log_path().parent, CONFIG_PATH.parent)
        self.assertEqual(_default_gui_log_path().name, "gui.log")


class TestFormatSessions(unittest.TestCase):
    def test_none_shows_checking(self):
        self.assertIn("checking", format_sessions(None, "", watching=False, cfg=Config()))

    def test_none_shows_note(self):
        self.assertIn("not running", format_sessions(None, "iTerm2 not running?", watching=False, cfg=Config()))

    def test_empty(self):
        self.assertIn("none found", format_sessions([], "", watching=False, cfg=Config()))

    def test_not_watching_lists_status_without_affect(self):
        out = format_sessions([("A", "working"), ("B", "idle")], "", watching=False, cfg=Config())
        self.assertIn("working", out)
        self.assertIn("idle", out)
        self.assertIn("A", out)
        self.assertNotIn("will resume", out)
        self.assertNotIn("skipped", out)

    def test_watching_marks_idle_resume_and_busy_skipped(self):
        out = format_sessions([("A", "working"), ("B", "idle")], "", watching=True, cfg=Config())
        self.assertIn("will resume", out)       # idle session
        self.assertIn("skipped (busy)", out)    # working session, skip_busy default

    def test_watching_force_resumes_busy(self):
        out = format_sessions([("A", "working")], "", watching=True, cfg=Config(force=True))
        self.assertIn("will resume", out)
        self.assertNotIn("skipped", out)

    def test_watching_no_skip_busy_resumes_busy(self):
        out = format_sessions([("A", "working")], "", watching=True, cfg=Config(skip_busy=False))
        self.assertIn("will resume", out)

    def test_watching_exec_mode_is_headless(self):
        out = format_sessions([("A", "idle")], "", watching=True, cfg=Config(exec_cmd="claude -p go"))
        self.assertIn("headless", out)
        self.assertNotIn("will resume", out)

    def test_long_list_truncated(self):
        many = [("S%d" % i, "idle") for i in range(12)]
        out = format_sessions(many, "", watching=False, cfg=Config())
        self.assertIn("...and 4 more", out)  # 12 - 8


class TestWatchExplanation(unittest.TestCase):
    def test_default_explains_continue_to_idle_and_skip_busy(self):
        out = watch_explanation(Config())
        self.assertIn("window to reset", out)
        self.assertIn("continue", out)            # the default text it sends
        self.assertIn("idle Claude sessions", out)
        self.assertIn("Busy sessions are skipped", out)

    def test_quota_mode_describes_opening_a_window(self):
        out = watch_explanation(Config(start_window=True))
        self.assertIn("window", out)
        self.assertIn("headlessly", out)
        self.assertNotIn("sends", out)  # not the resume/broadcast wording

    def test_exec_mode_describes_headless_command(self):
        out = watch_explanation(Config(exec_cmd="claude -p go"))
        self.assertIn("claude -p go", out)
        self.assertIn("headlessly", out)
        self.assertNotIn("Busy sessions", out)    # no iTerm broadcast in exec mode

    def test_session_targeted(self):
        out = watch_explanation(Config(session="work"))
        self.assertIn("work", out)
        self.assertIn("session", out)

    def test_force_includes_busy(self):
        out = watch_explanation(Config(force=True))
        self.assertIn("force is on", out)

    def test_skip_busy_off(self):
        out = watch_explanation(Config(skip_busy=False))
        self.assertIn("skip-busy is off", out)

    def test_keystroke_on_windows_describes_window_not_iterm(self):
        with _ForcePlatform("windows"):
            out = watch_explanation(Config(keystroke=True, window_title="My Term"))
        self.assertIn("My Term", out)
        self.assertNotIn("iTerm2", out)
        self.assertNotIn("Busy sessions", out)   # keystroke has no skip-busy concept

    def test_keystroke_all_on_windows_describes_every_session(self):
        with _ForcePlatform("windows"):
            out = watch_explanation(Config(keystroke_all=True))
        self.assertIn("every running Claude session", out)
        self.assertNotIn("iTerm2", out)

    def test_keystroke_on_macos_falls_through_to_iterm(self):
        # keystroke is a no-op on macOS (action routes to the iTerm broadcast)
        with _ForcePlatform("macos"):
            out = watch_explanation(Config(keystroke=True))
        self.assertIn("iTerm2", out)

    def test_tmux_wins_over_keystroke_on_windows(self):
        with _ForcePlatform("windows"):
            out = watch_explanation(Config(keystroke=True, tmux=True))
        self.assertIn("tmux", out)


class TestEffectiveCfg(unittest.TestCase):
    def test_windows_defaults_to_keystroke(self):
        with _ForcePlatform("windows"):
            self.assertTrue(effective_cfg(Config()).keystroke)

    def test_windows_defaults_to_continue_all(self):
        # native Windows continues EVERY running Claude session (the panel lists
        # them), not just one window
        with _ForcePlatform("windows"):
            self.assertTrue(effective_cfg(Config()).keystroke_all)

    def test_wsl_defaults_to_single_keystroke_not_all(self):
        # WSL can't enumerate the Linux Claude proc, so it stays single-window
        with _ForcePlatform("wsl"):
            cfg = effective_cfg(Config())
            self.assertTrue(cfg.keystroke)
            self.assertFalse(cfg.keystroke_all)

    def test_wsl_defaults_to_keystroke(self):
        with _ForcePlatform("wsl"):
            self.assertTrue(effective_cfg(Config()).keystroke)

    def test_macos_unchanged(self):
        with _ForcePlatform("macos"):
            self.assertFalse(effective_cfg(Config()).keystroke)

    def test_configured_exec_not_overridden(self):
        with _ForcePlatform("windows"):
            cfg = effective_cfg(Config(exec_cmd="claude -p go"))
        self.assertFalse(cfg.keystroke)  # exec is the action; don't force keystroke

    def test_configured_tmux_not_overridden(self):
        with _ForcePlatform("windows"):
            self.assertFalse(effective_cfg(Config(tmux=True)).keystroke)

    def test_does_not_mutate_input(self):
        with _ForcePlatform("windows"):
            original = Config()
            effective_cfg(original)
        self.assertFalse(original.keystroke)  # replace() returns a copy

    def test_explanation_on_windows_default_is_not_iterm(self):
        # The GUI applies effective_cfg before explaining, so a zero-config Windows
        # user sees the continue-all wording, never "iTerm2".
        with _ForcePlatform("windows"):
            out = watch_explanation(effective_cfg(Config()))
        self.assertNotIn("iTerm2", out)
        self.assertIn("every running Claude session", out)  # continues all, not one window


class TestWinInstancesMode(unittest.TestCase):
    def test_true_on_native_windows(self):
        with _ForcePlatform("windows"):
            self.assertTrue(win_instances_mode(Config()))

    def test_false_when_tmux(self):
        with _ForcePlatform("windows"):
            self.assertFalse(win_instances_mode(Config(tmux=True)))

    def test_false_on_macos(self):
        with _ForcePlatform("macos"):
            self.assertFalse(win_instances_mode(Config()))

    def test_false_on_wsl(self):
        # WSL's Claude is a Linux process Win32_Process can't see.
        with _ForcePlatform("wsl"):
            self.assertFalse(win_instances_mode(Config()))


class TestFormatInstances(unittest.TestCase):
    def test_none_shows_checking(self):
        self.assertIn("checking", format_instances(None, ""))

    def test_none_shows_note(self):
        self.assertIn("failed", format_instances(None, "instance list query failed: x"))

    def test_empty_says_none_running(self):
        self.assertIn("none running", format_instances([], ""))

    def test_lists_instances_with_count_and_pid(self):
        out = format_instances([("claude", "22108"), ("claude", "35552")], "")
        self.assertIn("Claude instances (2):", out)
        self.assertIn("22108", out)
        self.assertIn("35552", out)
        self.assertIn("claude", out)

    def test_node_instance_reads_as_claude(self):
        # an npm node-based instance is still a Claude instance — label it so.
        out = format_instances([("node", "900")], "")
        self.assertIn("claude (node)", out)

    def test_long_list_truncated(self):
        many = [("claude", str(i)) for i in range(12)]
        out = format_instances(many, "")
        self.assertIn("...and 4 more", out)

    def test_not_watching_has_no_affect_annotation(self):
        out = format_instances([("claude", "22108")], "", watching=False)
        self.assertNotIn("will continue", out)

    def test_watching_marks_each_instance_will_continue(self):
        out = format_instances([("claude", "22108"), ("claude", "35552")], "", watching=True)
        self.assertEqual(out.count("-> will continue"), 2)  # both get a continue


class TestWatchingNote(unittest.TestCase):
    def test_nothing_to_show(self):
        self.assertEqual(watching_note(None, None, 0), ("", None))

    def test_fired_only_is_green_confirmation(self):
        fired = datetime(2026, 6, 16, 14, 30)
        text, color = watching_note(None, fired, 3)
        self.assertIn("last fired 14:30", text)
        self.assertIn("(3 total)", text)
        self.assertEqual(color, "#2a2")

    def test_warning_newer_than_fire_wins(self):
        fired = datetime(2026, 6, 16, 14, 30)
        warn = (fired + timedelta(minutes=1), "fire failed: nothing attached")
        text, color = watching_note(warn, fired, 3)
        self.assertIn("⚠", text)
        self.assertIn("nothing attached", text)
        self.assertEqual(color, "#a00")

    def test_older_warning_does_not_mask_a_later_fire(self):
        fired = datetime(2026, 6, 16, 14, 30)
        warn = (fired - timedelta(minutes=1), "earlier hiccup")  # before the fire
        text, color = watching_note(warn, fired, 1)
        self.assertIn("last fired", text)
        self.assertEqual(color, "#2a2")

    def test_warning_with_no_fire_shows(self):
        warn = (datetime(2026, 6, 16, 14, 30), "boom")
        text, color = watching_note(warn, None, 0)
        self.assertIn("boom", text)
        self.assertEqual(color, "#a00")


class TestShouldAnnotateContinue(unittest.TestCase):
    def test_only_when_watching_continue_all_and_not_quota(self):
        self.assertTrue(should_annotate_continue(True, False, True))

    def test_false_when_not_watching(self):
        self.assertFalse(should_annotate_continue(False, False, True))

    def test_false_in_quota_mode(self):
        # quota just opens a window; it does NOT continue the listed PIDs
        self.assertFalse(should_annotate_continue(True, True, True))

    def test_false_when_not_continue_all(self):
        self.assertFalse(should_annotate_continue(True, False, False))


class TestShouldAutoRecheck(unittest.TestCase):
    def test_first_check_always_allowed(self):
        self.assertTrue(should_auto_recheck(None, 1000.0))

    def test_debounced_within_interval(self):
        self.assertFalse(should_auto_recheck(1000.0, 1000.0 + 60, min_interval=120))

    def test_allowed_after_interval(self):
        self.assertTrue(should_auto_recheck(1000.0, 1000.0 + 120, min_interval=120))
        self.assertTrue(should_auto_recheck(1000.0, 1000.0 + 500, min_interval=120))


class TestUpdateButtonColor(unittest.TestCase):
    def test_available_is_green(self):
        info = UpdateInfo("0.5.0", "v0.6.0", True, "a.zip", "https://x")
        self.assertEqual(update_button_color(info, frozen=True), _BTN_UPDATE_AVAILABLE)

    def test_up_to_date_is_gray(self):
        info = UpdateInfo("0.5.0", "v0.5.0", False, None, None)
        self.assertEqual(update_button_color(info, frozen=True), _BTN_UP_TO_DATE)

    def test_source_install_is_gray(self):
        # newer exists but not installable from source -> gray, not green
        info = UpdateInfo("0.5.0", "v0.6.0", True, "a.zip", "https://x")
        self.assertEqual(update_button_color(info, frozen=False), _BTN_UP_TO_DATE)

    def test_error_or_unknown_is_none(self):
        self.assertIsNone(update_button_color(None, frozen=True))
        err = UpdateInfo("0.5.0", None, False, None, None, error="boom")
        self.assertIsNone(update_button_color(err, frozen=True))


class TestUpdateButtonLabel(unittest.TestCase):
    # macOS ignores button colour, so the label carries a colour glyph
    def test_available_shows_green_glyph(self):
        info = UpdateInfo("0.5.0", "v0.6.0", True, "a.zip", "https://x")
        self.assertIn("🟢", update_button_label(info, frozen=True))

    def test_up_to_date_shows_check(self):
        info = UpdateInfo("0.5.0", "v0.5.0", False, None, None)
        self.assertIn("✓", update_button_label(info, frozen=True))
        self.assertNotIn("🟢", update_button_label(info, frozen=True))

    def test_unknown_is_plain(self):
        self.assertEqual(update_button_label(None, frozen=True), "⟳  Update")
        err = UpdateInfo("0.5.0", None, False, None, None, error="boom")
        self.assertNotIn("🟢", update_button_label(err, frozen=True))

    def test_source_with_newer_is_not_green(self):
        # newer exists but not installable from source -> not the green glyph
        info = UpdateInfo("0.5.0", "v0.6.0", True, "a.zip", "https://x")
        self.assertNotIn("🟢", update_button_label(info, frozen=False))


class TestUpdateDecision(unittest.TestCase):
    def test_none_info(self):
        kind, _ = update_decision(None, frozen=True)
        self.assertEqual(kind, "none")

    def test_error_surfaced(self):
        info = UpdateInfo("0.3.0", None, False, None, None, error="boom")
        kind, msg = update_decision(info, frozen=True)
        self.assertEqual(kind, "none")
        self.assertIn("boom", msg)

    def test_up_to_date(self):
        info = UpdateInfo("0.3.0", "v0.3.0", False, None, None)
        kind, msg = update_decision(info, frozen=True)
        self.assertEqual(kind, "none")
        self.assertIn("up to date", msg)

    def test_source_install_points_to_git(self):
        info = UpdateInfo("0.3.0", "v0.4.0", True, "a.zip", "https://x")
        kind, msg = update_decision(info, frozen=False)
        self.assertEqual(kind, "none")
        self.assertIn("git pull", msg)

    def test_no_asset_for_platform(self):
        info = UpdateInfo("0.3.0", "v0.4.0", True, None, None)
        kind, msg = update_decision(info, frozen=True)
        self.assertEqual(kind, "none")
        self.assertIn("no build", msg)

    def test_prompts_when_upgradable(self):
        info = UpdateInfo("0.3.0", "v0.4.0", True, "a.zip", "https://x")
        kind, _ = update_decision(info, frozen=True)
        self.assertEqual(kind, "prompt")


def _local_raw(h, m=0):
    """A tz-aware UTC datetime whose LOCAL wall-clock is h:m — so the offset/field
    helpers (which call .astimezone()) are deterministic regardless of the test
    machine's timezone."""
    return datetime(2026, 6, 14, h, m).astimezone().astimezone(timezone.utc)


class TestOffsetFromClock(unittest.TestCase):
    def test_later_time_is_positive_offset(self):
        # estimate resets 17:00 local; the real reset is 17:42 -> +42m correction
        self.assertEqual(offset_from_clock(_local_raw(17, 0), 17, 42), 42 * 60)

    def test_earlier_time_is_negative_offset(self):
        self.assertEqual(offset_from_clock(_local_raw(17, 30), 17, 10), -20 * 60)

    def test_zero_when_equal(self):
        self.assertEqual(offset_from_clock(_local_raw(9, 0), 9, 0), 0)

    def test_wraps_to_nearest_across_midnight(self):
        # estimate 23:50; 00:10 means 20 min LATER (next day), not 23h40m earlier
        self.assertEqual(offset_from_clock(_local_raw(23, 50), 0, 10), 20 * 60)

    def test_wraps_backward_across_midnight(self):
        # estimate 00:05; 23:55 means 10 min EARLIER (previous day), not ~24h later
        self.assertEqual(offset_from_clock(_local_raw(0, 5), 23, 55), -10 * 60)


class TestFormatResetField(unittest.TestCase):
    def test_none_waits_for_a_window(self):
        entry, hint = format_reset_field(None, 0)
        self.assertEqual(entry, "")
        self.assertIn("waiting", hint)

    def test_no_offset_shows_the_estimate(self):
        entry, hint = format_reset_field(_local_raw(17, 0), 0)
        self.assertEqual(entry, "17:00")
        self.assertIn("auto-estimate", hint)
        self.assertIn("17:00", hint)

    def test_offset_shifts_entry_and_explains_correction(self):
        entry, hint = format_reset_field(_local_raw(17, 0), 42 * 60)
        self.assertEqual(entry, "17:42")           # corrected fire time
        self.assertIn("17:00", hint)               # the raw estimate
        self.assertIn("+42m", hint)                # the applied correction
        self.assertIn("every reset", hint)         # reused, not one-shot

    def test_negative_offset_shows_signed_correction(self):
        entry, hint = format_reset_field(_local_raw(17, 30), -20 * 60)
        self.assertEqual(entry, "17:10")
        self.assertIn("-20m", hint)

    def test_sub_minute_offset_reads_as_estimate_not_plus_zero(self):
        # a nonzero correction that rounds to 0 minutes (e.g. a 20s CLI --reset-offset)
        # must NOT claim "+0m correction applied" — it reads as the plain estimate.
        entry, hint = format_reset_field(_local_raw(17, 0), 20)
        self.assertIn("auto-estimate", hint)
        self.assertNotIn("+0m", hint)
        self.assertNotIn("correction applied", hint)

    def test_field_round_trips_through_offset_from_clock(self):
        # typing the displayed entry text back in yields the same offset (the
        # property the GUI relies on so a Return on an unchanged field is a no-op)
        raw = _local_raw(14, 0)
        entry, _ = format_reset_field(raw, 42 * 60)
        hh, mm = (int(p) for p in entry.split(":"))
        self.assertEqual(offset_from_clock(raw, hh, mm), 42 * 60)


class TestOffsetFromClockDST(unittest.TestCase):
    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the timezone")
    def test_offset_is_real_elapsed_across_spring_forward(self):
        # The correction must be REAL elapsed seconds, not naive wall-clock delta:
        # building the target with .replace() on the estimate's fixed offset would be
        # an hour wrong across a DST seam. America/New_York springs 02:00->03:00 on
        # 2026-03-08, so 01:30 EST -> 03:30 EDT is only 1h of real time, not 2h.
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            raw = datetime(2026, 3, 8, 1, 30).astimezone().astimezone(timezone.utc)
            self.assertEqual(offset_from_clock(raw, 3, 30), 3600)  # 1h real, not 7200
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the timezone")
    def test_offset_is_real_elapsed_across_fall_back(self):
        # 2026-11-01 falls back 02:00->01:00; 00:30 EDT -> 03:30 EST is 4h of real time.
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            raw = datetime(2026, 11, 1, 0, 30).astimezone().astimezone(timezone.utc)
            self.assertEqual(offset_from_clock(raw, 3, 30), 4 * 3600)  # 4h real, not 3h
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to pin the timezone")
    def test_format_reset_field_round_trips_across_spring_forward(self):
        # the display must mirror offset_from_clock's re-localization: a correction to
        # 03:30 across the seam must RENDER as "03:30", not "02:30" from a naive
        # fixed-offset add (which would then overwrite the user's typed value).
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            raw = datetime(2026, 3, 8, 1, 30).astimezone().astimezone(timezone.utc)
            entry, _ = format_reset_field(raw, offset_from_clock(raw, 3, 30))
            self.assertEqual(entry, "03:30")  # not "02:30"
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()


class TestResetControlsState(unittest.TestCase):
    def test_idle_with_estimate_and_offset_enables_both(self):
        self.assertEqual(reset_controls_state(watching=False, has_estimate=True, offset=42 * 60),
                         (True, True))

    def test_watching_locks_everything(self):
        # settings apply at start, so the field + button are dead while watching
        self.assertEqual(reset_controls_state(watching=True, has_estimate=True, offset=42 * 60),
                         (False, False))

    def test_no_estimate_disables_the_field(self):
        # nothing to correct against yet -> field locked (button also dead at offset 0)
        self.assertEqual(reset_controls_state(watching=False, has_estimate=False, offset=0),
                         (False, False))

    def test_zero_offset_disables_only_the_use_estimate_button(self):
        # already on the estimate -> "use estimate" is a no-op, but the field stays open
        self.assertEqual(reset_controls_state(watching=False, has_estimate=True, offset=0),
                         (True, False))


class TestPickFamily(unittest.TestCase):
    def test_picks_first_installed_preferred(self):
        avail = {"Consolas", "Arial", "Segoe UI"}
        self.assertEqual(_pick_family(avail, ("Segoe UI", "Arial"), "Fallback"), "Segoe UI")

    def test_skips_missing_and_takes_next_preferred(self):
        avail = {"Arial"}
        self.assertEqual(_pick_family(avail, ("Segoe UI", "Arial"), "Fallback"), "Arial")

    def test_falls_back_when_none_installed(self):
        # graceful fallback: no preferred family present -> Tk's own default family
        self.assertEqual(_pick_family(set(), ("Segoe UI", "Arial"), "TkDefault"), "TkDefault")


class TestParseResetInput(unittest.TestCase):
    def test_no_estimate_yet_is_a_noop(self):
        # raw None -> (None, None): nothing to correct against, leave the field as-is
        self.assertEqual(parse_reset_input(None, "17:42"), (None, None))

    def test_empty_field_is_a_noop_not_a_clear(self):
        # empty (or whitespace) -> (None, None): a blur/alt-tab mid-edit must NOT wipe
        # a good correction; clearing is the explicit "use estimate" button instead.
        self.assertEqual(parse_reset_input(_local_raw(17, 0), ""), (None, None))
        self.assertEqual(parse_reset_input(_local_raw(17, 0), "   "), (None, None))

    def test_valid_time_parses_to_signed_offset(self):
        offset, error = parse_reset_input(_local_raw(17, 0), "17:42")
        self.assertEqual(offset, 42 * 60)
        self.assertIsNone(error)

    def test_valid_earlier_time_is_negative(self):
        offset, error = parse_reset_input(_local_raw(17, 30), "17:10")
        self.assertEqual(offset, -20 * 60)
        self.assertIsNone(error)

    def test_invalid_time_returns_error_not_offset(self):
        for bad in ["nope", "25:00", "9", "17:99", "1742"]:
            offset, error = parse_reset_input(_local_raw(17, 0), bad)
            self.assertIsNone(offset, bad)
            self.assertIsNotNone(error, bad)


if __name__ == "__main__":
    unittest.main()
