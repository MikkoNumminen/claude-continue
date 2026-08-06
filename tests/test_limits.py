import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
from _support import utc

from claude_continue import limits


def entry(**kw):
    """One transcript line. Defaults to a plain assistant turn."""
    base = {"type": "assistant", "timestamp": "2026-08-05T03:49:30.278Z",
            "cwd": "D:\\koodaamista\\app", "isSidechain": False}
    base.update(kw)
    return json.dumps(base)


def limit_entry(text="You've hit your session limit \u00b7 resets 8:40am (Europe/Helsinki)",
                ts="2026-08-05T03:49:30.278Z"):
    return entry(timestamp=ts, isApiErrorMessage=True, error="rate_limit",
                 apiErrorStatus=429,
                 message={"role": "assistant", "content": [{"type": "text", "text": text}]})


def assistant_entry(ts="2026-08-05T06:00:00.000Z"):
    return entry(timestamp=ts,
                 message={"role": "assistant", "content": [{"type": "text", "text": "done"}]})


def user_entry(text="continue", ts="2026-08-05T05:41:30.000Z"):
    return entry(type="user", timestamp=ts, message={"role": "user", "content": text})


class TestProjectSlug(unittest.TestCase):
    def test_windows_path_flattens_like_claude_code(self):
        # The real folder for D:\koodaamista\LuokkaretkiGenerator, observed on disk.
        self.assertEqual(limits.project_slug("D:\\koodaamista\\LuokkaretkiGenerator"),
                         "D--koodaamista-LuokkaretkiGenerator")

    def test_dots_in_a_folder_name_become_dashes(self):
        self.assertEqual(
            limits.project_slug("D:\\koodaamista\\mikkonumminen.dev\\.claude\\worktrees\\audio"),
            "D--koodaamista-mikkonumminen-dev--claude-worktrees-audio")

    def test_posix_path(self):
        self.assertEqual(limits.project_slug("/home/mikko/app"), "-home-mikko-app")

    def test_trailing_separator_is_not_part_of_the_name(self):
        self.assertEqual(limits.project_slug("D:\\koodaamista\\app\\"),
                         limits.project_slug("D:\\koodaamista\\app"))

    def test_empty_cwd_is_empty(self):
        self.assertEqual(limits.project_slug("   "), "")


class TestLimitKind(unittest.TestCase):
    def test_session(self):
        self.assertEqual(limits.limit_kind("You've hit your session limit \u00b7 resets 8:40am"), "session")

    def test_weekly(self):
        self.assertEqual(limits.limit_kind("You've hit your weekly limit \u00b7 resets 10am"), "weekly")

    def test_model_cap_is_its_own_kind(self):
        # A model cap is NOT cleared by typing `continue`, so it must not read as a
        # resumable session limit.
        self.assertEqual(limits.limit_kind("You've reached your Fable 5 limit. Run /usage-credits"),
                         "model")
        self.assertEqual(limits.limit_kind("You've hit your Opus limit"), "model")

    def test_unrelated_api_error_is_not_a_limit(self):
        self.assertEqual(limits.limit_kind("API Error: Connection closed mid-response."), "")

    def test_empty(self):
        self.assertEqual(limits.limit_kind(""), "")


class TestParseResetClock(unittest.TestCase):
    def test_am_with_minutes(self):
        self.assertEqual(limits.parse_reset_clock("resets 8:40am (Europe/Helsinki)"), (8, 40))

    def test_pm_with_minutes(self):
        self.assertEqual(limits.parse_reset_clock("resets 9:40pm (Europe/Helsinki)"), (21, 40))

    def test_bare_hour(self):
        self.assertEqual(limits.parse_reset_clock("resets 10am (Europe/Helsinki)"), (10, 0))

    def test_noon_and_midnight(self):
        self.assertEqual(limits.parse_reset_clock("resets 12am"), (0, 0))
        self.assertEqual(limits.parse_reset_clock("resets 12pm"), (12, 0))

    def test_no_clause(self):
        self.assertIsNone(limits.parse_reset_clock("You've hit your Opus limit"))

    def test_out_of_range_is_rejected(self):
        self.assertIsNone(limits.parse_reset_clock("resets 19:40am"))


class TestResolveReset(unittest.TestCase):
    def test_same_day_when_the_clock_is_still_ahead(self):
        recorded = utc(2026, 8, 5, 3, 49)  # 06:49 in UTC+3
        got = limits.resolve_reset((8, 40), recorded)
        self.assertGreater(got, recorded)
        self.assertLess(got - recorded, timedelta(hours=24))
        self.assertEqual(got.astimezone().strftime("%H:%M"), "08:40")

    def test_rolls_to_tomorrow_when_the_clock_already_passed(self):
        recorded = utc(2026, 8, 5, 20, 0)
        got = limits.resolve_reset((3, 0), recorded)
        self.assertGreater(got, recorded)
        self.assertEqual(got.astimezone().strftime("%H:%M"), "03:00")

    def test_never_returns_a_past_instant(self):
        for hour in range(24):
            recorded = utc(2026, 8, 5, hour, 17)
            self.assertGreaterEqual(limits.resolve_reset((6, 0), recorded), recorded)


class TestStateFromLines(unittest.TestCase):
    def test_limit_as_the_last_outcome_means_blocked(self):
        st = limits.state_from_lines([assistant_entry("2026-08-05T02:00:00Z"), limit_entry()])
        self.assertTrue(st.known)
        self.assertTrue(st.limited)
        self.assertEqual(st.kind, "session")
        self.assertIsNotNone(st.reset_at)

    def test_our_own_injected_continue_does_not_look_like_a_resume(self):
        # The keystroke lands in the transcript as a user turn BEFORE Claude has
        # accepted or refused it. Treating that as proof of a resume would report
        # success the instant we typed.
        st = limits.state_from_lines([limit_entry(), user_entry()])
        self.assertTrue(st.limited)

    def test_a_real_assistant_turn_after_the_limit_means_resumed(self):
        st = limits.state_from_lines([limit_entry(), user_entry(), assistant_entry()])
        self.assertTrue(st.known)
        self.assertFalse(st.limited)

    def test_sidechain_and_meta_entries_are_skipped(self):
        lines = [limit_entry(),
                 entry(isSidechain=True, timestamp="2026-08-05T07:00:00Z",
                       message={"role": "assistant", "content": "sub-agent output"}),
                 entry(isMeta=True, timestamp="2026-08-05T07:01:00Z",
                       message={"role": "assistant", "content": "meta"})]
        self.assertTrue(limits.state_from_lines(lines).limited)

    def test_other_api_errors_are_not_limits(self):
        lines = [limit_entry(),
                 entry(timestamp="2026-08-05T07:00:00Z", isApiErrorMessage=True,
                       message={"role": "assistant",
                                "content": [{"type": "text", "text": "API Error: Connection closed"}]})]
        st = limits.state_from_lines(lines)
        self.assertTrue(st.known)
        self.assertFalse(st.limited)

    def test_truncated_json_line_is_ignored_not_fatal(self):
        st = limits.state_from_lines([limit_entry(), '{"type": "assis'])
        self.assertTrue(st.limited)

    def test_no_decisive_entry_is_unknown_not_unlimited(self):
        # "we could not tell" must never be mistaken for "not limited" — the gate
        # treats them differently.
        st = limits.state_from_lines([user_entry(), ""])
        self.assertFalse(st.known)
        self.assertFalse(st.limited)

    def test_empty_transcript(self):
        self.assertFalse(limits.state_from_lines([]).known)


class TestClearedSession(unittest.TestCase):
    """`/clear` opens a FRESH transcript in the same terminal, so the project's
    newest file can legitimately hold no assistant turn at all.

    Observed live: a /clear at 23:47:53, 43 seconds after the previous session's
    last entry. Read as UNKNOWN it made the whole project unreadable and the gate
    held the terminal back. The tempting repair — fall back to the previous
    transcript — is worse: that one is the PRE-clear session, and acting on its
    spent limit would type `continue` into a freshly cleared terminal, the exact
    harm the gate exists to prevent. The right answer is that a session with no
    work in it has nothing to resume.
    """

    def _cleared(self):
        # the real shape: session-start markers, a meta user entry, the /clear
        # local-command echo, and nothing else
        return [entry(type="mode", timestamp=None),
                entry(type="file-history-snapshot", timestamp=None),
                entry(type="user", isMeta=True, timestamp="2026-08-05T20:47:53Z"),
                entry(type="user", timestamp="2026-08-05T20:47:53Z",
                      message={"role": "user", "content": "<local-command-caveat>…"}),
                entry(type="system", subtype="local_command", timestamp="2026-08-05T20:47:53Z")]

    def test_a_complete_read_with_no_assistant_turn_is_fresh_not_unknown(self):
        st = limits.state_from_lines(self._cleared(), complete=True)
        self.assertTrue(st.known, "a cleared session is an answer, not a blank")
        self.assertFalse(st.limited)
        self.assertEqual(st.kind, "fresh")

    def test_fresh_is_never_resumed(self):
        st = limits.state_from_lines(self._cleared(), complete=True)
        self.assertFalse(st.resumable(utc(2026, 8, 5, 23)))

    def test_a_truncated_read_stays_unknown(self):
        # not having reached the file start is the OTHER reason to find nothing;
        # calling that "fresh" would wave through a session we never really read
        st = limits.state_from_lines(self._cleared(), complete=False)
        self.assertFalse(st.known)
        self.assertEqual(st.kind, "")

    def test_an_assistant_turn_still_wins_over_freshness(self):
        st = limits.state_from_lines(self._cleared() + [assistant_entry()], complete=True)
        self.assertEqual(st.kind, "")
        self.assertFalse(st.limited)

    def test_a_limit_still_wins_over_freshness(self):
        st = limits.state_from_lines(self._cleared() + [limit_entry()], complete=True)
        self.assertTrue(st.limited)


class TestReadTailCompleteness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "t.jsonl"

    def test_a_whole_small_file_reads_complete(self):
        self.path.write_text("a\nb\n", encoding="utf-8")
        lines, complete = limits.read_tail(self.path)
        self.assertEqual(lines, ["a", "b"])
        self.assertTrue(complete)

    def test_a_windowed_read_reports_incomplete(self):
        body = "\n".join("line%04d" % i for i in range(400)) + "\n"
        self.path.write_text(body, encoding="utf-8")
        _lines, complete = limits.read_tail(self.path, max_bytes=100)
        self.assertFalse(complete)

    def test_an_unreadable_file_is_not_complete(self):
        _lines, complete = limits.read_tail(Path("nope.jsonl"))
        self.assertFalse(complete)


class TestResumable(unittest.TestCase):
    def test_ready_once_the_reset_has_passed(self):
        st = limits.LimitState(known=True, limited=True, kind="session",
                               reset_at=utc(2026, 8, 5, 5, 40))
        self.assertFalse(st.resumable(utc(2026, 8, 5, 5, 0)))
        self.assertTrue(st.waiting(utc(2026, 8, 5, 5, 0)))
        self.assertTrue(st.resumable(utc(2026, 8, 5, 5, 40)))
        self.assertFalse(st.waiting(utc(2026, 8, 5, 5, 40)))

    def test_model_cap_is_never_resumable(self):
        st = limits.LimitState(known=True, limited=True, kind="model", reset_at=None)
        self.assertFalse(st.resumable(utc(2026, 8, 5, 12)))

    def test_limit_without_a_reset_time_is_treated_as_arrived(self):
        st = limits.LimitState(known=True, limited=True, kind="session", reset_at=None)
        self.assertTrue(st.resumable(utc(2026, 8, 5, 12)))

    def test_unknown_is_not_resumable(self):
        self.assertFalse(limits.UNKNOWN.resumable(utc(2026, 8, 5, 12)))

    def test_not_limited_is_not_resumable(self):
        self.assertFalse(limits.NOT_LIMITED.resumable(utc(2026, 8, 5, 12)))


class TestStaleLimits(unittest.TestCase):
    """A project's newest transcript is the PREVIOUS session's file until a freshly
    opened `claude` writes its first turn. If that old session ended parked on a
    long-spent limit, acting on it would type `continue` into a brand-new session —
    exactly the harm the gate exists to prevent. Age the limit out instead."""

    NOW = utc(2026, 8, 5, 12)

    def _state(self, reset_at=None, recorded_at=None):
        return limits.LimitState(known=True, limited=True, kind="session",
                                 reset_at=reset_at, recorded_at=recorded_at)

    def test_a_recent_spent_limit_is_still_resumable(self):
        st = self._state(reset_at=self.NOW - timedelta(hours=4))
        self.assertFalse(st.stale(self.NOW))
        self.assertTrue(st.resumable(self.NOW))

    def test_a_day_old_limit_is_stale_and_not_resumed(self):
        st = self._state(reset_at=self.NOW - timedelta(hours=25))
        self.assertTrue(st.stale(self.NOW))
        self.assertFalse(st.resumable(self.NOW))

    def test_an_overnight_gap_still_resumes(self):
        # PC off overnight: parked at 23:00, reset 04:00, user back at 09:00.
        st = self._state(reset_at=utc(2026, 8, 5, 1), recorded_at=utc(2026, 8, 4, 20))
        self.assertTrue(st.resumable(utc(2026, 8, 5, 6)))

    def test_dated_from_recorded_when_there_is_no_reset_time(self):
        st = self._state(recorded_at=self.NOW - timedelta(hours=30))
        self.assertTrue(st.stale(self.NOW))
        self.assertFalse(st.resumable(self.NOW))

    def test_no_reference_at_all_is_never_stale(self):
        # better to act than to strand a session we cannot date
        st = self._state()
        self.assertFalse(st.stale(self.NOW))
        self.assertTrue(st.resumable(self.NOW))

    def test_a_future_reset_is_waiting_not_stale(self):
        st = self._state(reset_at=self.NOW + timedelta(hours=1))
        self.assertFalse(st.stale(self.NOW))
        self.assertTrue(st.waiting(self.NOW))


class TestTailLines(unittest.TestCase):
    def test_reads_only_the_tail_and_drops_the_partial_first_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text("\n".join("line%03d" % i for i in range(500)) + "\n", encoding="utf-8")
            lines = limits.tail_lines(path, max_bytes=100)
            self.assertLess(len(lines), 500)
            self.assertEqual(lines[-1], "line499")
            # the first line read was mid-record and was dropped
            self.assertTrue(all(ln.startswith("line") for ln in lines))

    def test_whole_file_when_it_fits(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(limits.tail_lines(path), ["a", "b", "c"])

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(limits.tail_lines(Path("nope-does-not-exist.jsonl")), [])


class TestOversizedEntries(unittest.TestCase):
    """A bounded tail read can slice the decisive entry in half, and dropping it as
    a partial line reports "unreadable" for a session we could answer for. Real
    transcripts here hold single JSONL lines of 2.5 MB, and Claude Code appends a
    small `system` entry after each turn — so the window holds one skippable entry
    and half the answer. Held-forever is the mild consequence; the sharp one is that
    an all-unreadable snapshot sends the watch loop back to the ccusage signal and
    the re-fire storm."""

    def setUp(self):
        limits._CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, *entries):
        d = self.root / "D--koodaamista-app"
        d.mkdir(parents=True, exist_ok=True)
        (d / "s.jsonl").write_text("\n".join(entries) + "\n", encoding="utf-8")

    def _big(self, text_bytes, ts="2026-08-05T06:00:00Z"):
        return entry(timestamp=ts, message={"role": "assistant",
                                            "content": [{"type": "text", "text": "x" * text_bytes}]})

    def _system(self, ts="2026-08-05T06:00:01Z"):
        return entry(type="system", subtype="turn_duration", timestamp=ts)

    def test_an_entry_larger_than_the_tail_window_still_answers(self):
        self._write(self._big(limits.TAIL_BYTES * 2), self._system())
        st = limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        self.assertTrue(st.known, "an oversized turn made the session unreadable")
        self.assertFalse(st.limited)

    def test_an_oversized_turn_before_a_limit_still_reports_the_limit(self):
        self._write(self._big(limits.TAIL_BYTES * 2), limit_entry(), self._system())
        st = limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        self.assertTrue(st.limited)
        self.assertIsNotNone(st.reset_at)

    def test_a_small_decision_less_file_is_fresh_not_unknown(self):
        # read whole, no assistant turn anywhere: that IS the answer (cleared or
        # newly started), not a failure to read
        self._write(*[user_entry(ts="2026-08-05T06:00:00Z") for _ in range(3)])
        st = limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        self.assertTrue(st.known)
        self.assertEqual(st.kind, "fresh")

    def test_a_huge_decision_less_file_stays_unknown(self):
        # the widened read is still bounded: past WIDE_TAIL_BYTES we never reach the
        # file start, so we cannot claim the session is fresh
        self._write(self._big(limits.WIDE_TAIL_BYTES + 1024), self._system())
        st = limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        self.assertFalse(st.known)

    def test_a_small_file_never_triggers_the_wide_read(self):
        reads = []
        real = limits.read_tail

        def spy(path, max_bytes=limits.TAIL_BYTES):
            reads.append(max_bytes)
            return real(path, max_bytes)

        self._write(assistant_entry())
        with mock.patch.object(limits, "read_tail", spy):
            limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        self.assertEqual(reads, [limits.TAIL_BYTES])


class TestStateCache(unittest.TestCase):
    """Both callers ask repeatedly — the GUI polls every 5s while watching and the
    verifier re-reads on every retry — so an unchanged transcript must not be
    re-parsed (and a widened 8 MB read must not be repeated) for no new answer."""

    def setUp(self):
        limits._CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dir = self.root / "D--koodaamista-app"
        self.dir.mkdir(parents=True)
        self.path = self.dir / "s.jsonl"

    def _write(self, *entries, mtime=None):
        self.path.write_text("\n".join(entries) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(self.path, (mtime, mtime))

    def test_unchanged_file_is_read_once(self):
        self._write(limit_entry(), mtime=1000)
        with mock.patch.object(limits, "read_tail", wraps=limits.read_tail) as spy:
            for _ in range(5):
                limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        self.assertEqual(spy.call_count, 1)

    def test_a_changed_file_is_re_read(self):
        # the whole point: a session that resumes must stop reporting a limit
        self._write(limit_entry(), mtime=1000)
        self.assertTrue(limits.state_for_cwd("D:\\koodaamista\\app", root=self.root).limited)
        self._write(limit_entry(), user_entry(), assistant_entry(), mtime=2000)
        self.assertFalse(limits.state_for_cwd("D:\\koodaamista\\app", root=self.root).limited)

    def test_same_mtime_but_different_size_is_re_read(self):
        # an append landing inside one filesystem timestamp tick must not be missed
        self._write(limit_entry(), mtime=1000)
        self.assertTrue(limits.state_for_cwd("D:\\koodaamista\\app", root=self.root).limited)
        self._write(limit_entry(), user_entry(), assistant_entry(), mtime=1000)
        self.assertFalse(limits.state_for_cwd("D:\\koodaamista\\app", root=self.root).limited)

    def test_cache_stays_bounded(self):
        for i in range(limits._CACHE_LIMIT + 20):
            p = self.dir / ("s%d.jsonl" % i)
            p.write_text(limit_entry() + "\n", encoding="utf-8")
            limits._read_state(p)
        self.assertLessEqual(len(limits._CACHE), limits._CACHE_LIMIT)

    def test_cache_does_not_pin_the_transcript_contents(self):
        # Caching the line list would hold up to WIDE_TAIL_BYTES of decoded strings
        # per entry — hundreds of megabytes across the cache, for the life of the
        # process. Only the two small extracted values belong in there.
        self._write(limit_entry(), mtime=1000)
        limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        for cached in limits._CACHE.values():
            for value in cached:
                self.assertNotIsInstance(value, (list, tuple, dict, bytes))


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        limits._CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _project(self, slug, lines, name="s.jsonl", mtime=None):
        d = self.root / slug
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_state_for_cwd_finds_the_project_folder(self):
        self._project("D--koodaamista-app", [limit_entry()])
        st = limits.state_for_cwd("D:\\koodaamista\\app", root=self.root)
        self.assertTrue(st.limited)
        self.assertTrue(st.path.endswith("s.jsonl"))

    def test_folder_matched_case_insensitively(self):
        # the same directory appears as both "D--" and "d--" depending on how the
        # drive letter was typed when the session started
        self._project("d--koodaamista-app", [limit_entry()])
        self.assertTrue(limits.state_for_cwd("D:\\koodaamista\\app", root=self.root).limited)

    def test_missing_project_is_unknown(self):
        self.assertFalse(limits.state_for_cwd("D:\\nope", root=self.root).known)

    def test_newest_transcript_wins(self):
        self._project("D--koodaamista-app", [limit_entry()], name="old.jsonl", mtime=1000)
        self._project("D--koodaamista-app", [assistant_entry()], name="new.jsonl", mtime=2000)
        self.assertFalse(limits.state_for_cwd("D:\\koodaamista\\app", root=self.root).limited)

    def test_subagent_sidechains_are_not_candidates(self):
        self._project("D--koodaamista-app", [limit_entry()], name="main.jsonl", mtime=1000)
        sub = self.root / "D--koodaamista-app" / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "agent-x.jsonl").write_text(assistant_entry() + "\n", encoding="utf-8")
        os.utime(sub / "agent-x.jsonl", (9000, 9000))
        self.assertTrue(limits.state_for_cwd("D:\\koodaamista\\app", root=self.root).limited)

    def test_recent_states_skips_stale_projects(self):
        now = utc(2026, 8, 5, 12)
        fresh = now.timestamp() - 600
        stale = now.timestamp() - 5 * 24 * 3600
        live = json.dumps({**json.loads(limit_entry()), "cwd": "D:\\koodaamista\\live"})
        gone = json.dumps({**json.loads(limit_entry()), "cwd": "D:\\koodaamista\\abandoned"})
        self._project("D--koodaamista-live", [live], mtime=fresh)
        self._project("D--koodaamista-abandoned", [gone], mtime=stale)
        found = limits.recent_states(now=now, root=self.root)
        self.assertEqual([cwd for cwd, _st in found], ["D:\\koodaamista\\live"])

    def test_recent_states_reports_the_recorded_cwd(self):
        now = utc(2026, 8, 5, 12)
        self._project("D--koodaamista-app", [limit_entry()], mtime=now.timestamp() - 60)
        found = limits.recent_states(now=now, root=self.root)
        self.assertEqual(found[0][0], "D:\\koodaamista\\app")

    def test_missing_root_is_empty_not_an_error(self):
        self.assertEqual(limits.recent_states(now=utc(2026, 8, 5), root=self.root / "gone"), [])


class TestSummarise(unittest.TestCase):
    def test_counts_each_bucket(self):
        now = utc(2026, 8, 5, 12)
        states = [
            ("app", limits.LimitState(known=True, limited=True, kind="session",
                                      reset_at=now - timedelta(minutes=5))),
            ("web", limits.LimitState(known=True, limited=True, kind="session",
                                      reset_at=now + timedelta(hours=1))),
            ("cli", limits.NOT_LIMITED),
        ]
        text = limits.summarise(states, now)
        self.assertIn("1 ready (app)", text)
        self.assertIn("1 waiting", text)
        self.assertIn("1 not limited", text)

    def test_no_sessions(self):
        self.assertEqual(limits.summarise([], utc(2026, 8, 5)), "no sessions")

    def test_stale_is_named_not_folded_into_not_limited(self):
        now = utc(2026, 8, 5, 12)
        states = [("old", limits.LimitState(known=True, limited=True, kind="session",
                                            reset_at=now - timedelta(days=2)))]
        text = limits.summarise(states, now)
        self.assertIn("1 stale (old)", text)
        self.assertNotIn("not limited", text)


class TestStatePreservesEveryField(unittest.TestCase):
    def test_state_for_cwd_keeps_all_fields_when_attaching_the_path(self):
        # Guards against the field-by-field rebuild this replaced: adding a field to
        # LimitState must not silently drop it on the way out of state_for_cwd.
        import dataclasses
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "D--koodaamista-app").mkdir(parents=True)
            (root / "D--koodaamista-app" / "s.jsonl").write_text(
                limit_entry() + "\n", encoding="utf-8")
            got = limits.state_for_cwd("D:\\koodaamista\\app", root=root)
        direct = limits.state_from_lines([limit_entry()])
        for f in dataclasses.fields(limits.LimitState):
            if f.name == "path":
                self.assertTrue(got.path)
                continue
            self.assertEqual(getattr(got, f.name), getattr(direct, f.name), f.name)


class TestTranscriptRoot(unittest.TestCase):
    def test_honours_claude_config_dir(self):
        root = limits.transcript_root(env={"CLAUDE_CONFIG_DIR": os.path.join("X", "cfg")})
        self.assertEqual(root, Path("X") / "cfg" / "projects")

    def test_defaults_under_home(self):
        root = limits.transcript_root(env={}, home=Path("H"))
        self.assertEqual(root, Path("H") / ".claude" / "projects")


if __name__ == "__main__":
    unittest.main()
