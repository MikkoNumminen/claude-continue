"""The ccusage block model.

A *block* is ccusage's reconstruction of a 5-hour usage window from the local
Claude Code transcript timestamps. Its ``end`` is the (estimated) reset time —
see the README's "estimate, not gospel" caveat for why ``watch`` verifies it
after firing rather than trusting it blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime.

    Python 3.9's ``datetime.fromisoformat`` rejects a trailing ``Z`` (that was
    fixed in 3.11), so we normalise it to ``+00:00`` first.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Block:
    """One ccusage usage block, normalised to tz-aware UTC datetimes."""

    id: str
    start: datetime  # block start, floored to the hour by ccusage
    end: datetime  # start + 5h: the estimated reset time
    actual_end: datetime | None  # timestamp of the last message seen (NOT the reset)
    is_active: bool
    is_gap: bool

    @property
    def reset_at(self) -> datetime:
        """The estimated reset instant for this window (UTC)."""
        return self.end

    @classmethod
    def from_json(cls, d: dict) -> Block:
        return cls(
            id=d["id"],
            start=parse_iso(d["startTime"]),
            end=parse_iso(d["endTime"]),
            actual_end=parse_iso(d["actualEndTime"]) if d.get("actualEndTime") else None,
            is_active=bool(d.get("isActive", False)),
            is_gap=bool(d.get("isGap", False)),
        )


def active_block_from_payload(payload: dict) -> Block | None:
    """Extract the active, non-gap block from a ccusage JSON payload.

    Works for both ``blocks --active`` (single block) and ``blocks --recent``
    (many blocks, including synthetic ``isGap`` ones). Returns ``None`` when
    there is no active window (idle), i.e. ``{"blocks": []}``.
    """
    for raw in payload.get("blocks") or []:
        if raw.get("isGap"):
            continue
        block = Block.from_json(raw)
        if block.is_active:
            return block
    return None
