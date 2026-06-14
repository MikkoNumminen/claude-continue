"""Compute *when* to fire.

Two strategies:
- ``next_target``: from a live ccusage block (reset + buffer). The default.
- ``fixed_target``: a clock-based fallback for when ccusage is unavailable or
  the user wants windows anchored to specific hours (``--at`` / ``--every``).
"""

from __future__ import annotations

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
        base = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if base > local_now:
            base -= timedelta(days=1)
        step = timedelta(hours=every_hours)
        target = base
        while target <= local_now:
            target += step
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
