"""Thin, defensive wrapper around the ``ccusage`` CLI.

``ccusage`` (https://github.com/ryoppippi/ccusage) reconstructs 5-hour usage
blocks from the local Claude Code transcripts. It is the only local source for
the active window's reset time — ``~/.claude.json`` and the ``claude`` CLI
expose nothing.

Everything here is built to *never crash the daemon*: any failure (missing
binary, timeout, nonzero exit, non-JSON output) surfaces as
``CcusageUnavailable`` so the caller can fall back to a fixed schedule or poll.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

from . import osenv
from .model import Block, active_block_from_payload

# ``--offline`` is mandatory: without it ccusage may block on a network pricing
# fetch and wedge the watch loop.
DEFAULT_CMD = ["npx", "ccusage", "blocks", "--active", "--json", "--offline"]

# Test/verification hook: set CLAUDE_CONTINUE_CCUSAGE_CMD to e.g.
#   "cat tests/fixtures/active.json"
# to feed canned JSON without invoking the real tool.
CMD_ENV = "CLAUDE_CONTINUE_CCUSAGE_CMD"


class CcusageUnavailable(Exception):
    """ccusage could not be run, or returned output we cannot use."""


def _command() -> list[str]:
    override = os.environ.get(CMD_ENV)
    argv = shlex.split(override) if override else list(DEFAULT_CMD)
    # resolve npx (and on Windows wrap the .cmd shim) so it runs without a shell
    return osenv.resolve_argv(argv)


def get_active_block(timeout: float = 30.0) -> Block | None:
    """Return the active usage block, or ``None`` when idle (no active window).

    Raises ``CcusageUnavailable`` on any failure — callers must treat that as
    "no signal", not as a crash.
    """
    cmd = _command()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise CcusageUnavailable(f"command not found: {cmd[0]!r}") from e
    except subprocess.TimeoutExpired as e:
        raise CcusageUnavailable(f"ccusage timed out after {timeout}s") from e
    except OSError as e:
        raise CcusageUnavailable(f"failed to run ccusage: {e}") from e

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise CcusageUnavailable(
            f"ccusage exited {proc.returncode}: {stderr[:200]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise CcusageUnavailable(f"ccusage produced non-JSON output: {e}") from e

    try:
        return active_block_from_payload(payload)
    except (KeyError, ValueError, TypeError) as e:
        # ccusage changed its JSON shape (renamed/removed fields, bad timestamp)
        raise CcusageUnavailable(f"unexpected ccusage JSON shape: {e}") from e
