"""Per-session limit state, read from Claude Code's own transcripts.

WHY THIS EXISTS
---------------
ccusage answers "when does the 5-hour block end?" by *reconstructing* blocks from
transcript timestamps: it floors the block start to the whole hour and adds five
hours. That estimate is often wrong, and — the part that actually bit us — it can
never confirm a resume that lands *before* the estimate. The resumed messages fall
inside the same five-hour bucket, so ccusage keeps reporting the same block and the
post-fire check reads "the window never rolled" while the session is in fact happily
working again. The watch loop then re-fires `continue` every two minutes into
sessions that were never blocked. Observed live on 2026-08-05: 62 `continue`s typed
into two healthy sessions across two windows.

Claude Code records the truth itself. When a session is cut off it appends an
assistant entry carrying ``isApiErrorMessage: true``, ``error: "rate_limit"`` and
text like::

    You've hit your session limit · resets 8:40am (Europe/Helsinki)

That is two things ccusage cannot give: whether THIS session is blocked right now,
and the real reset time straight from the server rather than a floor-to-the-hour
guess. This module reads it, so the watcher can answer "is there actually anything
to resume?" before typing into anyone's terminal.

CONTRACT
--------
Everything degrades to ``UNKNOWN`` rather than raising. A missing transcript root,
a renamed field, a half-written last line, a permission error: all of it must leave
the watch daemon running.

There are three answers, and callers must not collapse them:

- ``known=False`` (UNKNOWN)  — no signal; decide for yourself. Held back, never fired at.
- ``limited=False``          — looked, and this session is not blocked.
- ``kind="fresh"`` (FRESH)   — looked, and this session has no work in it at all:
  cleared with ``/clear``, or newly started. Not blocked and nothing to resume,
  but reported apart from plain "not limited" so the UI can say which it is.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from .model import parse_iso

# Claude Code stores one directory per project under its config dir, named after the
# working directory with the path punctuation flattened to "-" (see project_slug).
# CLAUDE_CONFIG_DIR relocates the whole config dir; honour it so a non-default
# install is still readable.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

# How far back from the end of a transcript we read. The decisive entry is the last
# assistant turn, and a single turn with large tool results can run past 64 KB, so
# leave real headroom — this is a bounded tail read, not a full-file parse.
TAIL_BYTES = 512 * 1024

# ...but a bounded read can slice the decisive entry in half, and then we drop it as
# a partial line and report "unreadable" for a session we could in fact answer for.
# That is not hypothetical: real transcripts here contain single JSONL lines of
# 2.5 MB (one large assistant turn), and Claude Code appends a small `system`
# turn_duration entry after each one — so the tail holds exactly one skippable
# entry and half of the answer. The consequences are the ones this module exists to
# prevent: an unreadable session is held back forever, and if every session goes
# unreadable the watch loop falls back to the ccusage signal and the old re-fire
# storm returns. So when the small window comes back with no answer AND it was
# truncated, widen once. Still bounded — a runaway file is never read whole.
WIDE_TAIL_BYTES = 8 * 1024 * 1024

# A transcript untouched for longer than this is a closed/abandoned session, not
# something waiting on a reset. Used only when scanning *all* projects (no live
# process list to narrow by).
STALE_AFTER = timedelta(hours=36)

# How long after its reset a parked session still counts as "resume this". A real
# window is five hours, so a genuine resume always lands well inside this; a limit
# whose reset passed a day ago is not a session waiting to be nudged.
#
# It also closes a hole that would reproduce the exact harm this module exists to
# prevent. We pick a project's NEWEST transcript, which is the previous session's
# file when a freshly-opened `claude` in that folder has not written its first turn
# yet. If that previous session happened to end parked on a long-spent limit, the
# gate would wave through a `continue` into a brand-new session that never asked
# for one. Ageing the limit out makes that stale answer harmless.
RESUME_WINDOW = timedelta(hours=12)

# The rate-limit flavours Claude Code reports. Only "session"/"weekly" are waiting
# for a clock reset; a model-specific cap ("You've hit your Opus limit", "You've
# reached your Fable 5 limit") is not resolved by typing `continue`, so it is
# reported under its own kind and never counts as resumable.
_KIND_PATTERNS = (
    ("session", re.compile(r"\bsession limit\b", re.I)),
    ("weekly", re.compile(r"\bweekly limit\b", re.I)),
    ("model", re.compile(r"\b(?:hit|reached) your [A-Z][\w.\- ]* limit\b")),
    ("limit", re.compile(r"\b(?:hit|reached) your limit\b", re.I)),
)

# "resets 8:40am (Europe/Helsinki)" / "resets 10pm (...)". The zone name is
# deliberately ignored: Claude Code prints the machine's own zone, which is the zone
# we localise into anyway, and pulling in a tz database (zoneinfo needs the `tzdata`
# wheel on Windows) would break the stdlib-only rule for no gain.
_RESET_RE = re.compile(r"\bresets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)


@dataclass(frozen=True)
class LimitState:
    """What one session's transcript says about being rate-limited.

    ``known`` False means we could not read a transcript at all — a different
    answer from "read it, not limited", and callers must not conflate them.
    """

    known: bool = False
    limited: bool = False
    kind: str = ""  # "session" | "weekly" | "model" | "limit" | ""
    reset_at: Optional[datetime] = None  # UTC; None when the text carried no time
    recorded_at: Optional[datetime] = None  # UTC timestamp of the limit entry
    path: str = ""  # transcript the answer came from (diagnostics)

    def resumable(self, now: datetime) -> bool:
        """True when this session is parked on a limit whose reset has arrived.

        A model cap is never resumable: ``continue`` cannot buy credits or switch
        models, so typing it just adds noise to a session the user has to deal with
        by hand. A limit with no parseable reset time is treated as arrived — the
        text told us the session is blocked and gave us nothing to wait for, and
        refusing forever would strand the session. A ``stale`` one is never
        resumable (see RESUME_WINDOW).
        """
        if not (self.known and self.limited) or self.kind == "model":
            return False
        if self.stale(now):
            return False
        return self.reset_at is None or now >= self.reset_at

    def waiting(self, now: datetime) -> bool:
        """True when blocked but the reset is still in the future.

        Kind-blind on purpose: it answers "is this session's reset ahead of us", not
        "will we act on it". Callers that schedule around a reset want ``capped``
        filtered out first — a model cap's reset is a fact about the model coming
        back, not a time this tool does anything at.
        """
        return bool(self.known and self.limited and self.reset_at is not None
                    and now < self.reset_at)

    @property
    def capped(self) -> bool:
        """True for a limit no ``continue`` can clear — today, a model cap.

        Blocked, but not blocked on anything we can wait out: it needs credits or a
        different model, both of which are the user's move. Counting one of these as
        a session we are waiting on makes the watcher re-arm on a reset that changes
        nothing for it, and (worse) keeps a fire from ever confirming, because there
        is always still "a session on a limit".
        """
        return bool(self.known and self.limited and self.kind == "model")

    def stale(self, now: datetime) -> bool:
        """True when this limit is too old to be a session waiting to be nudged.

        Dated from the reset when we have one, else from when the limit was
        recorded. No reference at all (a hand-built state, or a transcript entry
        with no timestamp) is never stale — better to act than to strand it.
        """
        reference = self.reset_at or self.recorded_at
        return reference is not None and (now - reference) > RESUME_WINDOW


UNKNOWN = LimitState()
NOT_LIMITED = LimitState(known=True)
# A session with no assistant turn anywhere in it: cleared with /clear, or newly
# started. Known and not limited — there is no paused work to resume — but kept
# distinct from NOT_LIMITED so the UI can say WHY there is nothing to do instead
# of implying the session is mid-flight.
FRESH = LimitState(known=True, kind="fresh")


# --- transcript discovery ----------------------------------------------------

def transcript_root(env: Optional[Mapping] = None, home: Optional[Path] = None) -> Path:
    """Directory holding Claude Code's per-project transcript folders."""
    source: Mapping = os.environ if env is None else env
    base = source.get(CONFIG_DIR_ENV)
    root = Path(base) if base else (home or Path.home()) / ".claude"
    return root / "projects"


def project_slug(cwd: str) -> str:
    """Claude Code's folder name for a session working in ``cwd``.

    It flattens the path's punctuation to "-", so ``D:\\koodaamista\\app`` becomes
    ``D--koodaamista-app`` (drive colon + separator giving the doubled dash) and
    ``mikkonumminen.dev`` becomes ``mikkonumminen-dev``. Pure, so the rule is
    testable on any platform. Case is preserved here and matched loosely by
    ``project_dir`` — the same directory shows up as both ``D--`` and ``d--``
    depending on how the drive letter was typed when the session started.
    """
    text = (cwd or "").strip()
    while text[-1:] in ("\\", "/"):  # a trailing separator is not part of the name
        text = text[:-1]
    return re.sub(r"[\\/:.]", "-", text)


def project_dir(cwd: str, *, root: Optional[Path] = None) -> Optional[Path]:
    """The transcript folder for ``cwd``, or None when there isn't one."""
    root = transcript_root() if root is None else root
    slug = project_slug(cwd)
    if not slug:
        return None
    exact = root / slug
    if exact.is_dir():
        return exact
    want = slug.casefold()
    try:
        for child in root.iterdir():
            if child.name.casefold() == want and child.is_dir():
                return child
    except OSError:
        return None
    return None


def newest_transcript(directory: Path) -> Optional[Path]:
    """The most recently written transcript in a project folder.

    Sub-agent sidechains live in a ``subagents/`` subdirectory and are deliberately
    not globbed: a sub-agent's transcript is not the session a keystroke would land
    in. Residual: two live sessions sharing one working directory are indistinguish-
    able here and the newer one answers for both.
    """
    try:
        files = [p for p in directory.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    best, best_mtime = None, -1.0
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


# --- transcript reading ------------------------------------------------------

def read_tail(path: Path, max_bytes: int = TAIL_BYTES):
    """``(lines, complete)`` from a bounded tail read of a JSONL file.

    ``complete`` is True when the read reached byte 0 — i.e. these lines ARE the
    whole file. That distinction carries the difference between the two reasons a
    scan can find no assistant turn: the window never reached one (say nothing), or
    there has never been one (a session with no work in it yet). Only the second is
    an answer, and without knowing which we had, both looked like "unreadable".

    A transcript grows without limit, so we never read the whole thing by default.
    The first line of a mid-file read is almost certainly a fragment, so it is
    dropped. Returns ``([], False)`` for anything unreadable — this is a diagnostic
    path, not a place to raise.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return ([], False)
    lines = data.decode("utf-8", "replace").splitlines()
    if start and lines:
        lines = lines[1:]
    return (lines, start == 0)


def _entry_text(entry: dict) -> str:
    """Flatten an entry's message content to plain text ('' when there is none)."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return " ".join(p for p in parts if p)
    return ""


def limit_kind(text: str) -> str:
    """Which limit a rate-limit message is about, '' when it isn't one. Pure."""
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(text or ""):
            return kind
    return ""


def parse_reset_clock(text: str) -> Optional[tuple]:
    """``(hour24, minute)`` from a "resets 8:40am" clause, or None. Pure."""
    m = _RESET_RE.search(text or "")
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if not (1 <= hour <= 12) or minute > 59:
        return None
    meridiem = m.group(3).lower()
    if hour == 12:
        hour = 0
    return (hour + 12 if meridiem == "pm" else hour, minute)


def resolve_reset(clock: Sequence, recorded_at: datetime) -> datetime:
    """Turn a wall-clock reset ("8:40am") into a UTC instant.

    The reset is always ahead of the message that announced it and always within a
    day of it, so the right instant is the first occurrence of that local wall-clock
    time at or after ``recorded_at``.

    DST-correct: candidates are built as NAIVE local times and localised with
    ``.astimezone()``, which reads the OS zone including the offset in force at that
    wall-clock time. Calling ``.replace(hour=...)`` on the aware timestamp instead
    would pin the *recorded* offset and land up to an hour off across a seam.
    """
    hour, minute = int(clock[0]), int(clock[1])
    naive = recorded_at.astimezone().replace(hour=hour, minute=minute, second=0,
                                             microsecond=0, tzinfo=None)
    for day in (0, 1):
        candidate = (naive + timedelta(days=day)).astimezone()
        if candidate >= recorded_at:
            return candidate.astimezone(timezone.utc)
    # Unreachable for a sane zone (a +1 day candidate is always later); fall back to
    # the recorded instant rather than returning None and complicating every caller.
    return recorded_at


def state_from_lines(lines: Iterable, *, complete: bool = False) -> LimitState:
    """Decide a session's limit state from its transcript lines. Pure.

    Scans BACKWARDS for the first entry that settles the question:

    - a rate-limit error   -> blocked, and its text carries the real reset time
    - any other API error  -> not blocked (the session failed for another reason)
    - a normal assistant turn -> not blocked (it produced work after any earlier limit)

    User entries are skipped on the way past: our own injected ``continue`` is
    recorded as a user turn, so treating one as decisive would read "resumed" the
    instant we typed, before Claude had a chance to accept or refuse it. Sidechain
    (sub-agent) and meta entries are skipped for the same reason — they describe a
    child of the session, not the session's own state.

    Finding nothing decisive means one of two things, and ``complete`` tells them
    apart. If these lines are the WHOLE file, the session has simply never produced
    an assistant turn — it was cleared with ``/clear`` (which opens a fresh
    transcript in the same terminal) or has only just started. That is an answer,
    not a blank: there is no paused work in it, so nothing to resume. Reported as
    FRESH. Without ``complete`` it is UNKNOWN, and the caller widens the read.

    Getting that wrong is not cosmetic. Treated as UNKNOWN, a cleared session made
    its whole project unreadable and the gate held it back; the tempting "fall back
    to the previous transcript" repair is worse still, because the previous one is
    the PRE-CLEAR session, and acting on its spent limit would type ``continue``
    into a freshly cleared terminal — the exact harm the gate exists to prevent.
    """
    for raw in reversed(list(lines)):
        raw = raw.strip()
        if not raw or raw[0] != "{":
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue  # a truncated or half-written line tells us nothing
        if not isinstance(entry, dict):
            continue
        if entry.get("isSidechain") is True or entry.get("isMeta") is True:
            continue
        if entry.get("isApiErrorMessage"):
            kind = limit_kind(_entry_text(entry))
            if not kind:
                return NOT_LIMITED  # some other API failure; nothing to wait for
            recorded_at = _timestamp(entry)
            reset_at = None
            if recorded_at is not None:
                clock = parse_reset_clock(_entry_text(entry))
                if clock is not None:
                    reset_at = resolve_reset(clock, recorded_at)
            return LimitState(known=True, limited=True, kind=kind,
                              reset_at=reset_at, recorded_at=recorded_at)
        if entry.get("type") == "assistant":
            return NOT_LIMITED
    return FRESH if complete else UNKNOWN


def _timestamp(entry: dict) -> Optional[datetime]:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return parse_iso(raw)
    except (ValueError, OverflowError):
        return None


# --- public lookups ----------------------------------------------------------

# Answers keyed by (path, mtime, size). A transcript is an append-only log, so
# content that has not changed cannot have a different answer — and both callers
# ask repeatedly: the GUI polls every 5s while watching, and the watch loop's
# verifier re-reads on every retry. Without this, an 8 MB widened read would be
# repeated thousands of times over an overnight run for no new information.
# Residual: an in-place rewrite that preserved BOTH mtime and size would be missed,
# which a JSONL append log does not do.
_CACHE: dict = {}
_CACHE_LIMIT = 64


def _read_state(path: Path):
    """``(LimitState, cwd)`` for one transcript, widening the read if a bounded tail
    could not answer. Cached on (mtime, size).

    The two small values are extracted here and the line list is dropped: caching
    the lines instead would pin up to ``WIDE_TAIL_BYTES`` of decoded strings per
    entry, which across the cache is hundreds of megabytes held for the life of the
    process.
    """
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return (UNKNOWN, "")
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    lines, complete = read_tail(path, TAIL_BYTES)
    state = state_from_lines(lines, complete=complete)
    # An unreadable answer from a window that did NOT reach the start of the file
    # may just be a decisive entry sliced in half (see WIDE_TAIL_BYTES). A FRESH
    # answer is already final — it can only come from a complete read.
    if not state.known and not complete:
        lines, complete = read_tail(path, WIDE_TAIL_BYTES)
        state = state_from_lines(lines, complete=complete)

    result = (replace(state, path=str(path)), _cwd_from_lines(lines))
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()  # crude but bounded; the working set is a handful of sessions
    _CACHE[key] = result
    return result


def state_for_cwd(cwd: str, *, root: Optional[Path] = None) -> LimitState:
    """Limit state of the session working in ``cwd`` (UNKNOWN when unreadable)."""
    directory = project_dir(cwd, root=root)
    if directory is None:
        return UNKNOWN
    path = newest_transcript(directory)
    if path is None:
        return UNKNOWN
    return _read_state(path)[0]


def recent_states(*, now: datetime, root: Optional[Path] = None,
                  stale_after: timedelta = STALE_AFTER) -> list:
    """``[(cwd, LimitState)]`` for every project whose transcript is recently active.

    The fallback for platforms where we cannot enumerate live Claude processes and
    read their working directories (everything except native Windows). Projects
    untouched for ``stale_after`` are skipped: an abandoned session that hit a limit
    last week must not keep a watcher firing forever.
    """
    root = transcript_root() if root is None else root
    cutoff = (now - stale_after).timestamp()
    out: List[Tuple[str, LimitState]] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return out
    for directory in children:
        if not directory.is_dir():
            continue
        path = newest_transcript(directory)
        if path is None:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        state, cwd = _read_state(path)
        if not state.known:
            continue
        out.append((cwd or directory.name, state))
    return out


def _cwd_from_lines(lines: Sequence) -> str:
    """The working directory a transcript records, '' when absent. Every entry
    carries ``cwd``, so the tail is enough — no need to re-read from the top."""
    for raw in reversed(list(lines)):
        raw = raw.strip()
        if not raw or raw[0] != "{":
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("cwd"), str):
            return entry["cwd"]
    return ""


def summarise(states: Sequence, now: datetime) -> str:
    """One-line description of a set of ``(label, LimitState)`` pairs, for logs."""
    if not states:
        return "no sessions"
    ready = [lbl for lbl, st in states if st.resumable(now)]
    # A model cap is reported on its own: it is stopped like a waiting session, but
    # no reset of ours frees it, so folding it into "waiting until 19:00" describes a
    # queue it is not in — and that line is what the logs and the doctor repeat back.
    capped = [lbl for lbl, st in states if st.capped and not st.stale(now)]
    waiting = [(lbl, st) for lbl, st in states
               if st.waiting(now) and not st.capped]
    # Counted apart from "not limited": a stale limit means we found one and aged
    # it out, which is a different thing to explain than a session that is working.
    stale = [lbl for lbl, st in states
             if st.limited and st.stale(now) and not st.waiting(now)]
    fresh = [lbl for lbl, st in states if st.kind == "fresh"]
    parts = []
    if ready:
        parts.append("%d ready (%s)" % (len(ready), ", ".join(sorted(ready))))
    if waiting:
        soonest = min(st.reset_at for _lbl, st in waiting if st.reset_at is not None)
        parts.append("%d waiting until %s"
                     % (len(waiting), soonest.astimezone().strftime("%H:%M")))
    if capped:
        parts.append("%d model-capped (%s)" % (len(capped), ", ".join(sorted(capped))))
    if stale:
        parts.append("%d stale (%s)" % (len(stale), ", ".join(sorted(stale))))
    if fresh:
        parts.append("%d cleared/new" % len(fresh))
    idle = (len(states) - len(ready) - len(waiting) - len(capped)
            - len(stale) - len(fresh))
    if idle:
        parts.append("%d not limited" % idle)
    return "; ".join(parts)
