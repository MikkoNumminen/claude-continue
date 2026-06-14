"""Compute *when* to fire.

Two strategies:
- ``next_target``: from a live ccusage block (reset + buffer). The default.
- ``fixed_target``: a clock-based fallback for when ccusage is unavailable or
  the user wants windows anchored to specific hours (``--at`` / ``--every``).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from .model import Block


def next_target(block: Block, buffer_seconds: int) -> datetime:
    """The instant to fire for a given active block: its reset + buffer (UTC).

    The buffer keeps us off the exact boundary, where the dying (exhausted)
    window may still be in effect.
    """
    return block.reset_at + timedelta(seconds=buffer_seconds)


def fixed_target(
    now: datetime,
    at: str | None = None,
    every_hours: float | None = None,
    anchor: str | None = None,
) -> datetime:
    """Next fire time from a fixed clock schedule, in the local timezone.

    - ``at="HH:MM"``: the next occurrence of that wall-clock time.
    - ``every_hours`` (+ optional ``anchor="HH:MM"``): the next multiple of
      ``every_hours`` past the anchor that lies strictly in the future.
    """
    local_now = now.astimezone()

    if at is not None:
        hh, mm = parse_hhmm(at)
        target = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target

    if every_hours is not None:
        if every_hours <= 0:
            raise ValueError("every_hours must be positive")
        hh, mm = parse_hhmm(anchor or "00:00")
        # Step from a FIXED wall-clock epoch (anchor HH:MM on a constant date),
        # using naive-local arithmetic, so the cadence is one continuous H-hour
        # grid that never restarts at the daily anchor. Re-anchoring per call-day
        # would inject an extra, off-schedule fire whenever H doesn't divide 24.
        step_seconds = every_hours * 3600.0
        now_naive = local_now.replace(tzinfo=None)
        ref_naive = datetime(2000, 1, 1, hh, mm)
        elapsed = (now_naive - ref_naive).total_seconds()
        steps = math.floor(elapsed / step_seconds) + 1  # first grid point strictly after now
        target = (ref_naive + timedelta(seconds=steps * step_seconds)).astimezone()
        # A grid point that lands in a DST spring-forward gap is a nonexistent
        # wall-clock time; astimezone() can localize it backward, leaving target
        # <= now. Step forward until it's genuinely in the future.
        while target <= local_now:
            steps += 1
            target = (ref_naive + timedelta(seconds=steps * step_seconds)).astimezone()
        return target

    raise ValueError("fixed_target requires either `at` or `every_hours`")


def parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM time: {s!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"time out of range: {s!r}")
    return hh, mm
