"""The self-rescheduling watch loop — the heart of claude-continue.

Cycle:
  1. Decide the next fire time.
       - If a fixed schedule is configured (``at`` / ``every_hours``), use it.
       - Else read the active ccusage block → fire at ``reset + reset_offset
         correction + buffer``.
       - Else (idle / ccusage unavailable) poll and retry.
  2. Sleep until the target, in ≤60s slices so we wake promptly after the Mac
     sleeps (a suspended ``sleep`` would otherwise overshoot by hours).
  3. Fire the action (broadcast ``continue`` / run the headless exec).
  4. Verify the window actually rolled (re-read ccusage). If it didn't —
     ccusage's reset estimate can be early — retry a bounded number of times.

All external effects (clock, sleep, ccusage, action) are injectable so the loop
can be unit-tested fast and offline.
"""

from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import action as action_mod
from . import ccusage as ccusage_mod
from . import schedule
from .config import Config, clamp_timing
from .lock import PidLock
from .log import get_logger
from .model import Block

# The injectable ports of the watch loop (real impls are the module defaults).
# See ARCHITECTURE.md "Ports & contracts".
Clock = Callable[[], datetime]
Sleeper = Callable[[float], object]                # return ignored (real one is Event.wait -> bool)
BlockGetter = Callable[[float], Optional[Block]]   # raises ccusage.CcusageUnavailable
Performer = Callable[..., list]                    # action.perform(cfg, dry_run=False)
Stop = Callable[[], bool]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


@dataclass
class _Plan:
    kind: str  # "fire" | "poll"
    target: datetime | None = None
    block: Block | None = None
    reason: str = ""


@dataclass
class _FireResult:
    """Outcome of one attempt to perform the action."""

    targets: Optional[list] = None  # acted-on labels; None means the fire errored
    gated: bool = False  # the limit gate declined — nothing was waiting on a reset
    detail: str = ""
    retry_at: Optional[datetime] = None  # when the gate says to come back

    @property
    def acted(self) -> bool:
        return self.targets is not None


def _fire(cfg: Config, perform: Performer, logger) -> _FireResult:
    """Perform the action, never letting a failure crash the daemon."""
    try:
        return _FireResult(targets=perform(cfg, dry_run=False))
    except action_mod.NothingToResume as e:
        # Not a failure: require_limit is on and no session is parked on a spent
        # limit. Logged at INFO because it is the common, correct outcome of a
        # scheduled check — the window rolled while the user was working normally.
        logger.info("nothing to resume (%s)", e.detail or "no session waiting on a limit")
        return _FireResult(gated=True, detail=e.detail, retry_at=e.retry_at)
    except Exception as e:  # noqa: BLE001 - a failed fire must degrade, not crash
        logger.warning("fire failed: %s", e)
        return _FireResult()


def _next_plan(cfg: Config, now: datetime, get_block: BlockGetter, logger,
               fired: Optional[dict] = None) -> _Plan:
    # A configured fixed schedule is treated as the primary trigger.
    if cfg.at or cfg.every_hours:
        target = schedule.fixed_target(
            now, at=cfg.at, every_hours=cfg.every_hours, anchor=cfg.anchor
        )
        return _Plan("fire", target=target, reason="fixed schedule")

    try:
        block = get_block(cfg.timeout)
    except ccusage_mod.CcusageUnavailable as e:
        logger.warning("ccusage unavailable: %s", e)
        return _Plan("poll", reason="ccusage unavailable")

    if block is None:
        # Quota mode wants a window OPEN; with none active, open one now. Resume
        # mode has nothing to resume when idle, so it just polls.
        if cfg.start_window:
            return _Plan("fire", target=now, reason="quota: no active window — opening one")
        return _Plan("poll", reason="idle (no active window)")

    already = (fired or {}).get(block.id, ())
    target = schedule.next_target(block, cfg.buffer, cfg.reset_offset)
    if cfg.reset_offset:
        corrected = block.reset_at + timedelta(seconds=cfg.reset_offset)
        reason = "reset %s (corrected to %s, %+dm)" % (
            _fmt(block.reset_at), _fmt(corrected), round(cfg.reset_offset / 60))
    else:
        reason = "reset %s" % _fmt(block.reset_at)

    if target in already:
        # The corrected time has already been tried for this window and didn't take.
        # A correction that fires EARLY otherwise burns the whole retry budget before
        # the real reset even arrives, and the window then expires with nothing done
        # (observed: a -80m correction gave up 16 minutes before the reset, leaving a
        # two-hour coverage gap), so the uncorrected reset gets its own attempt.
        #
        # Only when it is still AHEAD of the tried time, though: a late (positive)
        # correction leaves an uncorrected reset in the past, and firing that now
        # would be an extra `continue` carrying no new information — the spin-fire
        # this dedupe exists to prevent.
        raw = schedule.next_target(block, cfg.buffer, 0)
        if raw > target and raw not in already:
            return _Plan("fire", target=raw, block=block,
                         reason="%s — retrying at the uncorrected reset" % reason)
        return _Plan("poll", block=block, reason="window %s already handled" % block.id)
    return _Plan("fire", target=target, block=block, reason=reason)


def _sleep_until(target: datetime, *, clock: Clock, sleep: Sleeper, stop: Stop, slice_s: int = 60) -> str:
    """Sleep until ``target`` in small slices. Returns "reached" or "stopped"."""
    while True:
        if stop():
            return "stopped"
        remaining = (target - clock()).total_seconds()
        if remaining <= 0:
            return "reached"
        sleep(min(float(slice_s), remaining))


@dataclass
class _Verdict:
    """What the post-fire check concluded."""

    confirmed: bool = False
    # An instant the caller MUST re-arm on. Set when the sessions are still limited
    # and told us when they come back. The caller cannot derive this itself: by then
    # ccusage reports no active window at all (the estimate "ended" while the paused
    # session made no activity), and an idle poll never fires in resume mode — so
    # dropping this on the floor leaves the session parked until a human notices.
    retry_at: Optional[datetime] = None


def _verify_and_retry(cfg: Config, old_block: Optional[Block], *, clock: Clock, sleep: Sleeper,
                      get_block: BlockGetter, perform: Performer, logger, stop: Stop,
                      snapshot: Optional[Callable] = None) -> _Verdict:
    """After firing, confirm the resume actually took.

    Returns a ``_Verdict``. ``confirmed`` means the loop can move on; ``retry_at``
    is a time the caller MUST re-arm on and is never merely advisory.

    TWO SIGNALS, in priority order.

    1. The sessions themselves (``snapshot``). Claude Code writes a rate-limit entry
       into a session's transcript when it cuts it off, and stops writing them the
       moment the session resumes, so "is anything still parked on a limit?" is
       answerable directly and immediately. This is the signal that matters, because
       it is the only one that can distinguish "the resume worked" from "ccusage
       hasn't noticed yet".

    2. ccusage (the fallback, and the only signal in quota mode). Proof of a resume
       is a NEW window whose reset is later than the one we fired for.

       Why it is only a fallback: ccusage floors a block's start to the whole hour
       and calls the end five hours later. A resume that lands INSIDE that bucket —
       which is every resume, when the real reset arrives before the estimate —
       produces messages ccusage attributes to the SAME block. The check then reads
       "the window never rolled" forever while the sessions are working perfectly
       well, and the retry loop types `continue` into them every couple of minutes.

    QUOTA idle-open (``old_block`` None): we opened a window from idle; success is
    *any* active window appearing. A SINGLE check, no retry loop — each retry would
    spawn another ``claude -p``. If no window registered, an unconfirmed verdict
    tells the caller to back off at poll cadence.
    """
    attempts = 0
    while True:
        delay = cfg.verify_delay if attempts == 0 else cfg.retry_interval
        if _sleep_until(clock() + timedelta(seconds=delay), clock=clock, sleep=sleep, stop=stop) == "stopped":
            return _Verdict(confirmed=True)  # shutting down; nothing to back off on

        # --- signal 1: the sessions' own transcripts ---------------------------
        if snapshot is not None and old_block is not None:
            snap = snapshot()
            if snap.known:
                if not snap.blocked:
                    logger.info("resumed: no session is on a limit any more (%s)", snap.detail)
                    return _Verdict(confirmed=True)
                if not snap.ready:
                    # Still limited, but the reset hasn't arrived. Re-firing now is
                    # pure noise — Claude will refuse every one. Hand the real reset
                    # time back to the caller, which re-arms on it.
                    logger.info("still limited until %s; re-arming instead of retrying",
                                _fmt(snap.soonest) if snap.soonest else "an unknown time")
                    return _Verdict(retry_at=snap.soonest)
                # ready > 0: the reset has passed and a session is STILL parked, so
                # the keystroke genuinely didn't land. Fall through and retry.

        # --- signal 2: ccusage --------------------------------------------------
        try:
            new_block = get_block(cfg.timeout)
        except ccusage_mod.CcusageUnavailable as e:
            logger.warning("post-fire ccusage check failed: %s; assuming ok", e)
            return _Verdict(confirmed=True)
        if new_block is not None and (old_block is None or new_block.reset_at > old_block.reset_at):
            logger.info("window active: next reset %s", _fmt(new_block.reset_at))
            return _Verdict(confirmed=True)
        if old_block is None:
            # quota idle-open didn't register a window; don't hammer with re-opens
            logger.info("opened a window but none is active yet; will retry next cycle")
            return _Verdict()
        if attempts >= cfg.retry_cap:
            _log_give_up(cfg, old_block, clock(), logger)
            return _Verdict()
        attempts += 1
        logger.warning("still on the old window (retry %d/%d) — re-firing", attempts, cfg.retry_cap)
        result = _fire(cfg, perform, logger)
        if result.gated:
            # The gate closed mid-retry. Either the sessions resumed (done) or they
            # are limited again with a new reset — carry that time back rather than
            # burning the rest of the budget on refusals.
            logger.info("re-fire held: %s", result.detail or "nothing waiting on a limit")
            return _Verdict(confirmed=result.retry_at is None, retry_at=result.retry_at)
        logger.info("re-fired -> %s", result.targets if result.acted else "(fire failed)")


def _log_give_up(cfg: Config, old_block: Block, now: datetime, logger) -> None:
    """Explain a spent retry budget honestly.

    The retry budget starts at the FIRE, not at the reset, so a fire scheduled
    early (a negative ``reset_offset``) can burn all of it before the window is even
    due to roll — every check correctly saying "not yet" about a window with an hour
    left to run. Saying "quota coverage has lapsed" there is simply wrong, and it
    hides the actual fault: the fire time. Say which case this is.
    """
    minutes = (cfg.verify_delay + cfg.retry_cap * cfg.retry_interval) // 60
    if now < old_block.reset_at:
        remaining = int((old_block.reset_at - now).total_seconds() // 60)
        logger.warning(
            "stopping retries after %d attempts (~%dm): fired %dm before the window was "
            "due to roll (reset %s), so it never could have. Re-arming for the real "
            "reset — check the 'Fire at' time if this repeats.",
            cfg.retry_cap, minutes, remaining, _fmt(old_block.reset_at),
        )
        return
    logger.warning(
        "gave up after %d retries (~%dm): window never rolled — quota coverage "
        "has lapsed; will retry when ccusage next reports a window",
        cfg.retry_cap, minutes,
    )


def _rearm(retry_at: Optional[datetime], block: Optional[Block], now: datetime):
    """``(pending, pending_block)`` for a session-supplied re-arm time.

    Only a time strictly in the future is honoured: a past one would make the loop
    fire immediately and, if the sessions kept reporting it, spin. The block travels
    with it so the next fire is still verified against the window it belongs to.
    """
    if retry_at is None or retry_at <= now:
        return (None, None)
    return (retry_at, block)


def _prune(fired_targets: dict, keep: str) -> None:
    """Drop bookkeeping for windows other than the current one.

    Block ids are per-window, so without this the map grows for the life of the
    daemon. Keeping only the live window is enough: an old window can never come
    back, and a *new* one must be fired for regardless of what we did to its
    predecessor.
    """
    for key in [k for k in fired_targets if k != keep]:
        del fired_targets[key]


def run(
    cfg: Config,
    *,
    logger=None,
    clock: Optional[Clock] = None,
    sleep: Optional[Sleeper] = None,
    get_block: Optional[BlockGetter] = None,
    perform: Optional[Performer] = None,
    stop: Optional[Stop] = None,
    use_lock: bool = True,
    max_fires: Optional[int] = None,
    snapshot: Optional[Callable] = None,
) -> None:
    logger = logger or get_logger()
    clock = clock or _utc_now
    get_block = get_block or ccusage_mod.get_active_block
    # The default ports travel together: a caller that injects its own performer has
    # replaced the world the action runs in, so the limit snapshot — which reads that
    # same world's transcripts — must come from the caller too, not from the real
    # filesystem. Injecting one and inheriting the other is how a unit test ends up
    # scanning the developer's actual ~/.claude.
    real_action = perform is None
    perform = perform or action_mod.perform
    # The per-session limit view the verifier prefers over ccusage. Only meaningful
    # when require_limit is on: with it off the user has explicitly asked to fire
    # regardless of session state, so verification stays on the ccusage signal.
    if (snapshot is None and real_action and cfg.require_limit
            and not cfg.start_window and not cfg.exec_cmd):
        def snapshot():  # noqa: E306 - closes over cfg/clock
            return action_mod.snapshot(cfg, clock())

    # Floor any non-positive timing value so a fat-fingered config can't turn the
    # idle-poll / retry backoff into a busy loop (see config.clamp_timing).
    for name, value, floor in clamp_timing(cfg):
        logger.warning("%s=%r is below the %ds minimum; clamping", name, value, floor)

    # Default stop: an Event flipped by SIGTERM/SIGINT (launchd sends SIGTERM on
    # bootout). Using Event.wait as the sleeper means a signal interrupts the
    # sleep immediately, so the loop exits within launchd's grace period.
    event = None
    if stop is None:
        event = threading.Event()

        def _handler(signum, frame):
            event.set()

        # SIGBREAK is the Windows console-group signal (Ctrl-Break / taskkill);
        # include it so a launchd/Task-Scheduler stop ends the loop promptly.
        sigs = [signal.SIGTERM, signal.SIGINT]
        if hasattr(signal, "SIGBREAK"):
            sigs.append(signal.SIGBREAK)
        for sig in sigs:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # not in main thread (e.g. under test)
        stop = event.is_set
    if sleep is None:
        sleep = event.wait if event is not None else time.sleep

    lock = PidLock() if use_lock else None
    if lock is not None:
        lock.acquire()

    fires = 0  # fires that actually reached a session (the number worth reporting)
    attempts = 0  # every completed trip through the fire branch, what max_fires bounds
    # Which fire *times* have been tried for the current window, keyed by block id.
    # Tracking the times (not just "this block was handled") is what lets a window
    # whose corrected fire time missed get a second attempt at its real reset — the
    # single-flag version wrote the window off after one miss and left the reset
    # unattended, which is how an early fire turned into hours of dead time.
    fired_targets: dict = {}
    # An explicit re-arm the SESSIONS asked for (their own stated reset), overriding
    # whatever ccusage would suggest next. Nothing else can supply it: once the
    # estimate has passed, ccusage reports no active window, and an idle poll never
    # fires in resume mode — so a paused session would sit there indefinitely.
    pending: Optional[datetime] = None
    pending_block: Optional[Block] = None
    try:
        if cfg.exec_cmd:
            action_label = "exec"
        elif cfg.start_window:
            action_label = "open window (quota mode)"
        else:
            action_label = "send %r" % cfg.text
        logger.info("watch started (action: %s)", action_label)
        while not stop():
            if pending is not None:
                plan = _Plan("fire", target=pending, block=pending_block,
                             reason="the sessions' own stated reset")
                pending = pending_block = None
            else:
                plan = _next_plan(cfg, clock(), get_block, logger, fired_targets)

            if plan.kind == "poll":
                logger.info("%s; polling in %ds", plan.reason, cfg.poll_interval)
                if _sleep_until(clock() + timedelta(seconds=cfg.poll_interval), clock=clock, sleep=sleep, stop=stop) == "stopped":
                    break
                continue

            assert plan.target is not None  # a "fire" plan always carries a target
            logger.info("armed: fire at %s (%s)", _fmt(plan.target), plan.reason)
            if _sleep_until(plan.target, clock=clock, sleep=sleep, stop=stop) == "stopped":
                break

            result = _fire(cfg, perform, logger)
            attempts += 1
            if result.acted:
                fires += 1
                logger.info("fired -> %s", result.targets or "(no matching sessions)")
                if plan.block is not None:
                    # Only a real fire consumes this target. A gated or failed
                    # attempt leaves it available, so the same window can be tried
                    # again when the sessions are actually ready.
                    fired_targets.setdefault(plan.block.id, set()).add(plan.target)
                    _prune(fired_targets, plan.block.id)
                # Verify resume fires (block set) and quota opens-from-idle
                # (block None, but quota mode): confirm a window is active, retry
                # if not. plan.block may be None here — _verify_and_retry handles it.
                if plan.block is not None or cfg.start_window:
                    verdict = _verify_and_retry(
                        cfg, plan.block, clock=clock, sleep=sleep, get_block=get_block,
                        perform=perform, logger=logger, stop=stop, snapshot=snapshot,
                    )
                    pending, pending_block = _rearm(verdict.retry_at, plan.block, clock())
                    # quota opened from idle but no window registered: back off at
                    # poll cadence instead of re-opening back-to-back forever.
                    if plan.block is None and not verdict.confirmed:
                        if _sleep_until(clock() + timedelta(seconds=cfg.poll_interval),
                                        clock=clock, sleep=sleep, stop=stop) == "stopped":
                            break
            elif result.gated:
                # Nothing was waiting on a spent limit. Come back when the sessions
                # say they will be (their own stated reset beats any estimate), or at
                # poll cadence when they said nothing.
                pending, pending_block = _rearm(result.retry_at, plan.block, clock())
                if pending is None:
                    logger.info("nothing fired; polling in %ds", cfg.poll_interval)
                    if _sleep_until(clock() + timedelta(seconds=cfg.poll_interval),
                                    clock=clock, sleep=sleep, stop=stop) == "stopped":
                        break
                else:
                    logger.info("nothing fired; next attempt %s", _fmt(pending))
            else:
                # A failed fire is NOT a handled window — deliberately leaves the
                # target unconsumed, so the next cycle retries this same window.
                logger.warning("fire failed; retrying in %ds", cfg.retry_interval)

            # Bound on ATTEMPTS, not successful fires: a run whose every fire fails
            # (or is held by the limit gate) must still terminate.
            if max_fires is not None and attempts >= max_fires:
                logger.info("max_fires=%d reached; exiting", max_fires)
                break

            if not result.acted and not result.gated:
                # Back off before re-arming, otherwise the (now past) target would
                # re-fire in a tight loop.
                if _sleep_until(clock() + timedelta(seconds=cfg.retry_interval), clock=clock, sleep=sleep, stop=stop) == "stopped":
                    break
    finally:
        if lock is not None:
            lock.release()
    logger.info("watch stopped (fired %d time(s))", fires)
