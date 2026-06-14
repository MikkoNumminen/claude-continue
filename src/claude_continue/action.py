"""What to actually *do* at a reset.

Two actions, selected by config:
- default: broadcast ``continue`` into matching iTerm2 sessions (resumes the
  user's live, limit-paused sessions).
- ``exec_cmd`` set: run a headless command (e.g. ``claude -p '<task>'
  --permission-mode bypassPermissions``) detached — no terminal needed.
"""

from __future__ import annotations

import shlex
import subprocess

from . import iterm
from .config import Config


def perform(cfg: Config, dry_run: bool = False) -> list:
    """Execute the configured action. Returns a list of human-readable strings
    describing what was acted on (session names, or the exec command)."""
    if cfg.exec_cmd:
        return _run_exec(cfg.exec_cmd, dry_run=dry_run)
    return iterm.broadcast(
        cfg.text,
        cfg.filter,
        skip_busy=cfg.skip_busy,
        session=cfg.session,
        dry_run=dry_run,
        all_sessions=cfg.all_sessions,
        force=cfg.force,
        timeout=float(cfg.timeout),
    )


def _run_exec(command: str, dry_run: bool = False) -> list:
    label = "exec: " + command
    if dry_run:
        return [label]
    # Detach so the headless Claude run outlives this process and doesn't tie up
    # the watch loop. Output is discarded (the run has its own session log).
    subprocess.Popen(
        shlex.split(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return [label]
