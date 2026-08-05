import logging
import unittest
from datetime import timedelta

import _support  # noqa: F401
from _support import FakeClock, utc

from claude_continue import action as action_mod
from claude_continue import watch
from claude_continue.ccusage import CcusageUnavailable
from claude_continue.config import Config
from claude_continue.model import Block


def setUpModule():
    # Silence watch's log output for these tests — but scoped to this module and
    # restored after, so it doesn't leak into other modules (e.g. the GUI tests
    # whose fire-tap relies on logging being enabled).
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def block(idx, end):
    return Block(id="b%d" % idx, start=end - timedelta(hours=5), end=end,
                 actual_end=None, is_active=True, is_gap=False)


def cfg(**kw):
    base = dict(buffer=90, verify_delay=90, retry_cap=6, retry_interval=300, poll_interval=600)
    base.update(kw)
    return Config(**base)


class TestSleepUntil(unittest.TestCase):
    def test_reaches_target_in_slices(self):
        fc = FakeClock(utc(2026, 6, 14, 6))
        target = fc.now() + timedelta(seconds=130)
        slices = []
        orig = fc.sleep

        def rec(s):
            slices.append(s)
            orig(s)

        res = watch._sleep_until(target, clock=fc.now, sleep=rec, stop=lambda: False, slice_s=60)
        self.assertEqual(res, "reached")
        self.assertEqual(slices, [60, 60, 10])  # capped at 60s

    def test_wakes_after_overshoot(self):
        # Simulate the Mac sleeping: a single sleep jumps the clock far past target.
        fc = FakeClock(utc(2026, 6, 14, 6))
        target = fc.now() + timedelta(seconds=600)
        calls = []

        def jumpy(s):
            calls.append(s)
            fc.t += timedelta(seconds=3600)  # overshoot

        res = watch._sleep_until(target, clock=fc.now, sleep=jumpy, stop=lambda: False)
        self.assertEqual(res, "reached")
        self.assertEqual(len(calls), 1)  # detected overshoot after one slice, not one long sleep

    def test_stop_interrupts(self):
        fc = FakeClock(utc(2026, 6, 14, 6))
        target = fc.now() + timedelta(hours=5)
        res = watch._sleep_until(target, clock=fc.now, sleep=fc.sleep, stop=lambda: True)
        self.assertEqual(res, "stopped")


class TestWatchLoop(unittest.TestCase):
    def _run(self, *, get_block, perform, start, **cfgkw):
        fc = FakeClock(start)
        watch.run(cfg(**cfgkw), clock=fc.now, sleep=fc.sleep, get_block=get_block,
                  perform=perform, stop=lambda: False, use_lock=False, max_fires=1)
        return fc

    def test_happy_path_single_fire_at_reset_plus_buffer(self):
        T0 = utc(2026, 6, 14, 6)
        st = {"rolled": False}
        fired = []
        fc = FakeClock(T0 - timedelta(minutes=10))

        def gb(timeout=30):
            return block(2, T0 + timedelta(hours=5)) if st["rolled"] else block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["rolled"] = True  # firing rolls the window
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], T0 + timedelta(seconds=90))

    def test_reset_offset_shifts_fire_time(self):
        # a +42m correction makes the watch fire at reset + offset + buffer, not
        # reset + buffer — the GUI "Fire at" override flowing through the loop.
        T0 = utc(2026, 6, 14, 6)
        st = {"rolled": False}
        fired = []
        fc = FakeClock(T0 - timedelta(minutes=10))

        def gb(timeout=30):
            return block(2, T0 + timedelta(hours=5)) if st["rolled"] else block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["rolled"] = True
            return ["s"]

        watch.run(cfg(reset_offset=42 * 60), clock=fc.now, sleep=fc.sleep, get_block=gb,
                  perform=perform, stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], T0 + timedelta(seconds=42 * 60 + 90))

    def test_quota_mode_with_active_block_applies_offset(self):
        # both GUI buttons honour the correction: quota mode with an active window
        # fires at reset + offset + buffer too (not just the resume button).
        T0 = utc(2026, 6, 14, 6)
        st = {"rolled": False}
        fired = []
        fc = FakeClock(T0 - timedelta(minutes=10))

        def gb(timeout=30):
            return block(2, T0 + timedelta(hours=5)) if st["rolled"] else block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["rolled"] = True
            return ["open window"]

        watch.run(cfg(start_window=True, reset_offset=30 * 60), clock=fc.now, sleep=fc.sleep,
                  get_block=gb, perform=perform, stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], T0 + timedelta(seconds=30 * 60 + 90))

    def test_negative_offset_with_target_in_past_fires_promptly(self):
        # a -30m correction whose corrected target is already behind "now" must fire
        # at once (sleep_until returns "reached" immediately) — not hang or spin.
        T0 = utc(2026, 6, 14, 6)
        st = {"rolled": False}
        fired = []
        fc = FakeClock(T0)  # now == reset, so target = reset - 30m + 90s is in the past

        def gb(timeout=30):
            return block(2, T0 + timedelta(hours=5)) if st["rolled"] else block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["rolled"] = True
            return ["s"]

        watch.run(cfg(reset_offset=-30 * 60), clock=fc.now, sleep=fc.sleep, get_block=gb,
                  perform=perform, stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], T0)  # fired at "now", the past target didn't delay or loop

    def test_retries_until_window_rolls(self):
        T0 = utc(2026, 6, 14, 6)
        st = {"fires": 0}
        fired = []
        fc = FakeClock(T0 - timedelta(minutes=1))

        def gb(timeout=30):
            return block(9, T0 + timedelta(hours=5)) if st["fires"] >= 2 else block(1, T0)

        def perform(c, dry_run=False):
            st["fires"] += 1
            fired.append(fc.now())
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)
        self.assertGreaterEqual(len(fired), 2)

    def test_idle_polls_then_fires_when_window_appears(self):
        T0 = utc(2026, 6, 14, 6)
        st = {"reads": 0, "rolled": False}
        fired = []
        fc = FakeClock(T0)

        def gb(timeout=30):
            st["reads"] += 1
            if st["rolled"]:
                return block(2, T0 + timedelta(hours=10))
            if st["reads"] <= 2:
                return None  # idle
            return block(1, T0 + timedelta(hours=5))

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["rolled"] = True
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)

    def test_nonpositive_poll_interval_does_not_busy_loop(self):
        # A poll_interval of 0 (from a bad config/env value) must not spin: the
        # loop should clamp it and actually sleep between idle polls instead of
        # re-running ccusage every iteration with the clock frozen.
        T0 = utc(2026, 6, 14, 6)
        st = {"reads": 0, "rolled": False}
        fired = []
        fc = FakeClock(T0)
        sleeps = []

        def gb(timeout=30):
            st["reads"] += 1
            if st["rolled"]:
                return block(2, T0 + timedelta(hours=10))
            if st["reads"] <= 3:
                return None  # idle
            return block(1, T0 + timedelta(hours=5))

        def rec(s):
            sleeps.append(s)
            fc.sleep(s)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["rolled"] = True  # firing rolls the window so verify passes
            return ["s"]

        watch.run(cfg(poll_interval=0), clock=fc.now, sleep=rec, get_block=gb,
                  perform=perform, stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)
        # Each idle poll slept (clamped to >=1s), so the clock advanced and the
        # idle reads are bounded — not an unbounded spin.
        self.assertGreaterEqual(len(sleeps), 3)
        self.assertTrue(all(s >= 1 for s in sleeps))
        self.assertGreater(fc.now(), T0)

    def test_quota_mode_opens_window_when_idle(self):
        # quota mode + idle (no active window) -> open one NOW; verify sees it appear
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0)
        st = {"fires": 0}
        fired = []

        def gb(timeout=30):
            return block(1, T0 + timedelta(hours=5)) if st["fires"] >= 1 else None

        def perform(c, dry_run=False):
            st["fires"] += 1
            fired.append(fc.now())
            return ["open window"]

        watch.run(cfg(start_window=True), clock=fc.now, sleep=fc.sleep, get_block=gb,
                  perform=perform, stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)  # did NOT just poll — it opened a window

    def test_quota_idle_open_retries_across_cycles_until_window_appears(self):
        # first open doesn't register a window; the loop backs off and opens again
        # next cycle (poll-paced, NOT a tight re-fire loop) until one appears
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0)
        st = {"fires": 0}
        fired = []

        def gb(timeout=30):
            return block(1, T0 + timedelta(hours=5)) if st["fires"] >= 2 else None

        def perform(c, dry_run=False):
            st["fires"] += 1
            fired.append(fc.now())
            return ["open window"]

        watch.run(cfg(start_window=True), clock=fc.now, sleep=fc.sleep, get_block=gb,
                  perform=perform, stop=lambda: False, use_lock=False, max_fires=2)
        self.assertEqual(len(fired), 2)  # opened, didn't register, opened again -> registered

    def test_quota_idle_open_never_registers_is_poll_paced_not_spam(self):
        # regression: if opened windows NEVER register, don't re-open back-to-back;
        # each attempt is separated by ~poll_interval, not retry_interval.
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0)
        fired = []

        def gb(timeout=30):
            return None  # a window never appears

        def perform(c, dry_run=False):
            fired.append(fc.now())
            return ["open window"]

        # stop after ~25 min of fake time; with verify_delay=90 + poll_interval=600
        # between opens, that's only a couple of attempts — not dozens.
        watch.run(cfg(start_window=True, verify_delay=90, poll_interval=600, retry_cap=30),
                  clock=fc.now, sleep=fc.sleep, get_block=gb,
                  stop=lambda: fc.now() > T0 + timedelta(minutes=25), perform=perform, use_lock=False)
        self.assertLessEqual(len(fired), 4)  # poll-paced, bounded — not ~30/hr spam

    def test_dedupe_prevents_spin_on_unrolling_window(self):
        T0 = utc(2026, 6, 14, 6)
        fired = []
        fc = FakeClock(T0 - timedelta(seconds=30))

        def gb(timeout=30):
            return block(7, T0)  # never rolls

        def perform(c, dry_run=False):
            fired.append(fc.now())
            return ["s"]

        # no max_fires; stop once the clock has advanced well past any retries
        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: fc.now() > T0 + timedelta(hours=3), use_lock=False)
        # 1 initial fire + retry_cap (6) re-fires, then dedupe → no further fires
        self.assertEqual(len(fired), 7)

    def test_verify_ccusage_unavailable_after_fire_assumes_ok(self):
        # fire succeeds, but the post-fire ccusage check errors -> assume ok, no re-fire
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []
        st = {"fired": False}

        def gb(timeout=30):
            if st["fired"]:
                raise CcusageUnavailable("boom")
            return block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["fired"] = True
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)

    def test_none_after_fire_keeps_retrying_until_real_reset(self):
        # Regression for the early-fire bug: ccusage's estimate was early, so the
        # first `continue` fired BEFORE the real reset. ccusage then reports NO
        # active window (the estimate "ended" but the paused session makes no
        # activity). The loop must KEEP firing until the real reset produces a new
        # window — not treat that first None as success and give up.
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []
        st = {"fires": 0}

        def gb(timeout=30):
            if st["fires"] == 0:
                return block(1, T0)                       # arm on the (early) estimate
            if st["fires"] >= 3:
                return block(2, T0 + timedelta(hours=5))  # real reset finally rolled
            return None                                   # fired early; nothing active yet

        def perform(c, dry_run=False):
            st["fires"] += 1
            fired.append(fc.now())
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)
        # did NOT bail on the first None: re-fired until the window actually rolled
        self.assertEqual(len(fired), 3)

    def test_none_forever_gives_up_after_retry_cap(self):
        # if no window ever appears, the retries are still bounded by retry_cap
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []
        st = {"fired": False}

        def gb(timeout=30):
            return None if st["fired"] else block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            st["fired"] = True
            return ["s"]

        watch.run(cfg(retry_cap=4), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1 + 4)  # initial fire + retry_cap re-fires, then stop

    def test_early_fire_gets_a_second_attempt_at_the_real_reset(self):
        """Regression for the 2026-08-05 coverage gap.

        A -80m "Fire at" correction fired at 20:41 for a window resetting at 22:00.
        The retry budget (90s + 30x120s = 61m) ran out at 21:44, still 16 minutes
        before the reset it was waiting for, and the window was then written off as
        handled — so nothing fired at 22:00 either and the quota sat idle for hours.
        The corrected time missing must leave the real reset still armed.
        """
        T0 = utc(2026, 6, 14, 10)  # window reset
        early = timedelta(minutes=-80)
        fc = FakeClock(T0 - timedelta(hours=2))
        fired = []
        st = {"rolled": False}

        def gb(timeout=30):
            return block(1, T0 + timedelta(hours=5)) if st["rolled"] else block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            if fc.now() >= T0:  # only a fire at/after the real reset can take
                st["rolled"] = True
            return ["s"]

        watch.run(cfg(reset_offset=int(early.total_seconds()), retry_cap=2),
                  clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: fc.now() > T0 + timedelta(hours=2), use_lock=False)
        self.assertTrue(fired, "never fired at all")
        self.assertLess(fired[0], T0, "the corrected (early) time should be tried first")
        self.assertTrue(any(f >= T0 for f in fired),
                        "the real reset was never attempted after the early fire missed")

    def test_gave_up_message_names_an_early_fire_instead_of_blaming_quota(self):
        # Saying "quota coverage has lapsed" about a window with an hour left to run
        # is simply wrong, and it hides the actual fault (the fire time).
        T0 = utc(2026, 6, 14, 10)
        records = []

        class _Log:
            def info(self, msg, *a):
                records.append(("info", msg % a if a else msg))

            def warning(self, msg, *a):
                records.append(("warning", msg % a if a else msg))

        watch._log_give_up(cfg(), block(1, T0), T0 - timedelta(minutes=16), _Log())
        text = records[-1][1]
        self.assertIn("before the window was due to roll", text)
        self.assertNotIn("quota coverage has lapsed", text)

        records.clear()
        watch._log_give_up(cfg(), block(1, T0), T0 + timedelta(minutes=5), _Log())
        self.assertIn("quota coverage has lapsed", records[-1][1])

    def test_a_resumed_session_ends_verification_without_waiting_for_ccusage(self):
        # The core fix: ccusage cannot show a rolled window when the resume lands
        # inside its floored five-hour bucket, so it kept reporting the old block
        # while the sessions were working. The transcript signal answers directly.
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []

        def gb(timeout=30):
            return block(1, T0)  # ccusage never rolls — the whole point

        def perform(c, dry_run=False):
            fired.append(fc.now())
            return ["s"]

        def snapshot():
            return action_mod.Snapshot(known=True, ready=0, waiting=0, idle=2,
                                       detail="2 not limited")

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  snapshot=snapshot, stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)  # no re-fire storm

    def test_still_limited_re_arms_on_the_stated_reset_instead_of_retrying(self):
        """Re-firing at a session Claude is still refusing is pure noise. But the
        re-arm has to actually HAPPEN: by this point ccusage reports no active
        window (its estimate ended while the paused session made no activity) and an
        idle poll never fires in resume mode, so a dropped re-arm leaves the session
        parked indefinitely. Assert both halves: no retry storm, AND a fire at the
        session's own stated reset.
        """
        T0 = utc(2026, 6, 14, 6)
        ready_at = T0 + timedelta(minutes=40)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []
        st = {"limited": True}

        def gb(timeout=30):
            # after the estimate passes, ccusage sees nothing active — the paused
            # session generates no usage, so there is no window to report
            return block(1, T0) if fc.now() < T0 else None

        def perform(c, dry_run=False):
            fired.append(fc.now())
            if fc.now() >= ready_at:
                st["limited"] = False
            return ["s"]

        def snapshot():
            if st["limited"]:
                return action_mod.Snapshot(known=True, ready=0, waiting=1, idle=0,
                                           soonest=ready_at, detail="1 waiting")
            return action_mod.Snapshot(known=True, idle=1, detail="1 not limited")

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  snapshot=snapshot, stop=lambda: fc.now() > ready_at + timedelta(hours=1),
                  use_lock=False)
        self.assertEqual(len(fired), 2, "expected one fire, then one re-arm — got %s" % (fired,))
        self.assertEqual(fired[1], ready_at)  # exactly the session's stated reset

    def test_a_past_rearm_time_is_ignored_rather_than_spun_on(self):
        # A stale/past reset must not make the loop fire in a tight circle.
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []

        def gb(timeout=30):
            return block(1, T0)

        def perform(c, dry_run=False):
            fired.append(fc.now())
            return ["s"]

        def snapshot():
            return action_mod.Snapshot(known=True, ready=0, waiting=1, idle=0,
                                       soonest=T0 - timedelta(hours=9), detail="stale")

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  snapshot=snapshot, stop=lambda: fc.now() > T0 + timedelta(hours=3),
                  use_lock=False)
        self.assertEqual(len(fired), 1)

    def test_a_late_correction_does_not_earn_an_extra_immediate_fire(self):
        # The uncorrected-reset second chance exists for a correction that fired
        # EARLY. With a positive offset the uncorrected reset is already in the past,
        # so firing it would just be another `continue` carrying no new information.
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []

        def gb(timeout=30):
            return block(1, T0)  # never rolls

        def perform(c, dry_run=False):
            fired.append(fc.now())
            return ["s"]

        watch.run(cfg(reset_offset=3600, retry_cap=2), clock=fc.now, sleep=fc.sleep,
                  get_block=gb, perform=perform,
                  stop=lambda: fc.now() > T0 + timedelta(hours=4), use_lock=False)
        self.assertEqual(len(fired), 3)  # initial + 2 bounded retries, then dedupe
        self.assertTrue(all(f >= T0 + timedelta(hours=1) for f in fired))

    def test_unknown_snapshot_falls_back_to_the_ccusage_check(self):
        # No readable transcript must not disable verification entirely.
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        fired = []

        def gb(timeout=30):
            return block(1, T0)  # never rolls

        def perform(c, dry_run=False):
            fired.append(fc.now())
            return ["s"]

        watch.run(cfg(retry_cap=3), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  snapshot=lambda: action_mod.Snapshot(known=False), stop=lambda: False,
                  use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1 + 3)  # initial + retry_cap re-fires

    def test_a_gated_attempt_leaves_the_window_armed(self):
        # The gate declining is not the same as having fired: the window must stay
        # available so the next check can act on it once a session is actually ready.
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        calls = {"n": 0}

        def gb(timeout=30):
            return block(1, T0)

        def perform(c, dry_run=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise action_mod.NothingToResume("1 not limited")
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  snapshot=lambda: action_mod.Snapshot(known=True, idle=1),
                  stop=lambda: False, use_lock=False, max_fires=2)
        self.assertEqual(calls["n"], 2)  # retried the same window after the gate closed

    def test_gate_sleeps_until_the_sessions_stated_reset(self):
        T0 = utc(2026, 6, 14, 6)
        ready_at = T0 + timedelta(minutes=45)
        fc = FakeClock(T0 - timedelta(minutes=1))
        attempts = []

        def gb(timeout=30):
            return block(1, T0)

        def perform(c, dry_run=False):
            attempts.append(fc.now())
            raise action_mod.NothingToResume("1 waiting", retry_at=ready_at)

        watch.run(cfg(poll_interval=6000), clock=fc.now, sleep=fc.sleep, get_block=gb,
                  perform=perform, stop=lambda: False, use_lock=False, max_fires=2)
        # second attempt happens at the session's own reset, not a poll interval later
        self.assertEqual(attempts[1], ready_at)

    def test_fire_failure_does_not_crash_daemon(self):
        # perform raising must NOT propagate out of run(); the loop just re-arms
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))

        def gb(timeout=30):
            return block(1, T0)

        def perform(c, dry_run=False):
            raise RuntimeError("iTerm2 not running")

        # should complete normally (max_fires reached after the failed fire), no exception
        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)

    def test_loop_retries_window_after_a_failed_fire(self):
        # a transient failure must NOT mark the window handled; the next cycle
        # retries the SAME window and succeeds
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0 - timedelta(minutes=1))
        st = {"n": 0, "rolled": False}

        def gb(timeout=30):
            return block(2, T0 + timedelta(hours=5)) if st["rolled"] else block(1, T0)

        def perform(c, dry_run=False):
            st["n"] += 1
            if st["n"] == 1:
                raise RuntimeError("iTerm2 not running")  # first fire fails
            st["rolled"] = True  # second fire succeeds and rolls the window
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=2)
        self.assertEqual(st["n"], 2)  # retried after the failure and then succeeded

    def test_ccusage_unavailable_falls_to_poll(self):
        from claude_continue.ccusage import CcusageUnavailable
        T0 = utc(2026, 6, 14, 6)
        fc = FakeClock(T0)
        st = {"n": 0}
        fired = []

        def gb(timeout=30):
            st["n"] += 1
            if st["n"] <= 2:
                raise CcusageUnavailable("boom")
            return block(1, T0 + timedelta(hours=5)) if st["n"] == 3 else block(2, T0 + timedelta(hours=10))

        def perform(c, dry_run=False):
            fired.append(fc.now())
            return ["s"]

        watch.run(cfg(), clock=fc.now, sleep=fc.sleep, get_block=gb, perform=perform,
                  stop=lambda: False, use_lock=False, max_fires=1)
        self.assertEqual(len(fired), 1)


if __name__ == "__main__":
    unittest.main()
