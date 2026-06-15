"""The self-rescheduling watch loop — the heart of claude-continue.

Cycle:
  1. Decide the next fire time.
       - If a fixed schedule is configured (``at`` / ``every_hours``), use it.
       - Else read the active ccusage block → fire at ``reset + buffer``.
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

from . import action as action_mod
from . import ccusage as ccusage_mod
from . import schedule
from .config import Config, clamp_timing
from .lock import PidLock
from .log import get_logger
from .model import Block


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


def _fire(cfg, perform, logger):
    """Perform the action, never letting a failure crash the daemon.

    Returns the list of acted-on targets, or None if the fire failed (logged).
    """
    try:
        return perform(cfg, dry_run=False)
    except Exception as e:  # noqa: BLE001 - a failed fire must degrade, not crash
        logger.warning("fire failed: %s", e)
        return None


def _next_plan(cfg: Config, now: datetime, get_block, logger) -> _Plan:
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
        return _Plan("poll", reason="idle (no active window)")

    target = schedule.next_target(block, cfg.buffer)
    return _Plan("fire", target=target, block=block, reason="reset %s" % _fmt(block.reset_at))


def _sleep_until(target: datetime, *, clock, sleep, stop, slice_s: int = 60) -> str:
    """Sleep until ``target`` in small slices. Returns "reached" or "stopped"."""
    while True:
        if stop():
            return "stopped"
        remaining = (target - clock()).total_seconds()
        if remaining <= 0:
            return "reached"
        sleep(min(float(slice_s), remaining))


def _verify_and_retry(cfg, old_block, *, clock, sleep, get_block, perform, logger, stop) -> None:
    """After firing, confirm the window actually rolled; retry if it didn't.

    The only proof a resume *took* is a NEW active window whose reset is later
    than the one we fired for — resuming a session makes Claude work again, which
    creates a fresh ccusage block. Anything else means the resume did NOT land:

      - same block / earlier reset  -> ccusage was early; we fired before the
        real reset and the session is still limited.
      - NO active block             -> the early estimate "ended" the window in
        ccusage's view, but the paused session generates no activity, so there's
        nothing active yet. This is NOT success — it's the classic early-fire
        case; bailing here is exactly what left sessions un-resumed.

    So we keep re-firing `continue` on each check until a later window appears or
    we hit the retry cap. Verification happens at the top of every iteration, so
    even the last re-fire before giving up gets checked.
    """
    attempts = 0
    while True:
        delay = cfg.verify_delay if attempts == 0 else cfg.retry_interval
        if _sleep_until(clock() + timedelta(seconds=delay), clock=clock, sleep=sleep, stop=stop) == "stopped":
            return
        try:
            new_block = get_block(cfg.timeout)
        except ccusage_mod.CcusageUnavailable as e:
            logger.warning("post-fire ccusage check failed: %s; assuming ok", e)
            return
        if new_block is not None and new_block.reset_at > old_block.reset_at:
            logger.info("window rolled: next reset %s", _fmt(new_block.reset_at))
            return
        if attempts >= cfg.retry_cap:
            minutes = (cfg.verify_delay + cfg.retry_cap * cfg.retry_interval) // 60
            logger.warning(
                "gave up after %d retries (~%dm): window never rolled — quota coverage "
                "has lapsed; will resume only when ccusage next reports a new window",
                cfg.retry_cap, minutes,
            )
            return
        attempts += 1
        state = "no active window yet" if new_block is None else "still on the old window"
        logger.warning("%s (retry %d/%d) — re-firing", state, attempts, cfg.retry_cap)
        fired = _fire(cfg, perform, logger)
        logger.info("re-fired -> %s", fired if fired is not None else "(fire failed)")


def run(
    cfg: Config,
    *,
    logger=None,
    clock=None,
    sleep=None,
    get_block=None,
    perform=None,
    stop=None,
    use_lock: bool = True,
    max_fires=None,
) -> None:
    logger = logger or get_logger()
    clock = clock or _utc_now
    get_block = get_block or ccusage_mod.get_active_block
    perform = perform or action_mod.perform

    # Floor any non-positive loop interval so a fat-fingered config value can't
    # turn the idle-poll / retry backoff into a busy loop (see config.clamp_timing).
    for name, value, floor in clamp_timing(cfg):
        logger.warning("%s=%r is too low; clamping to %ds to avoid a busy loop", name, value, floor)

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

    fires = 0
    last_fired_block_id = None
    try:
        action_label = "exec" if cfg.exec_cmd else ("send %r" % cfg.text)
        logger.info("watch started (action: %s)", action_label)
        while not stop():
            plan = _next_plan(cfg, clock(), get_block, logger)

            if plan.kind == "poll":
                logger.info("%s; polling in %ds", plan.reason, cfg.poll_interval)
                if _sleep_until(clock() + timedelta(seconds=cfg.poll_interval), clock=clock, sleep=sleep, stop=stop) == "stopped":
                    break
                continue

            # Dedupe: don't re-arm a window we've already fired+retried for; wait
            # for it to roll (or a new one to appear) instead of spin-firing.
            if plan.block is not None and plan.block.id == last_fired_block_id:
                logger.info("window %s already handled; polling in %ds", plan.block.id, cfg.poll_interval)
                if _sleep_until(clock() + timedelta(seconds=cfg.poll_interval), clock=clock, sleep=sleep, stop=stop) == "stopped":
                    break
                continue

            logger.info("armed: fire at %s (%s)", _fmt(plan.target), plan.reason)
            if _sleep_until(plan.target, clock=clock, sleep=sleep, stop=stop) == "stopped":
                break

            fired = _fire(cfg, perform, logger)
            fires += 1
            if fired is not None:
                logger.info("fired -> %s", fired or "(no matching sessions)")
                if plan.block is not None:
                    last_fired_block_id = plan.block.id
                    _verify_and_retry(
                        cfg, plan.block, clock=clock, sleep=sleep, get_block=get_block,
                        perform=perform, logger=logger, stop=stop,
                    )
            else:
                # A failed fire is NOT a handled window — deliberately do not set
                # last_fired_block_id, so the next cycle retries this same window.
                logger.warning("fire failed; retrying in %ds", cfg.retry_interval)

            if max_fires is not None and fires >= max_fires:
                logger.info("max_fires=%d reached; exiting", max_fires)
                break

            if fired is None:
                # Back off before re-arming, otherwise the (now past) target would
                # re-fire in a tight loop.
                if _sleep_until(clock() + timedelta(seconds=cfg.retry_interval), clock=clock, sleep=sleep, stop=stop) == "stopped":
                    break
    finally:
        if lock is not None:
            lock.release()
    logger.info("watch stopped (fired %d time(s))", fires)
