"""Preflight checks: will claude-continue actually work on this machine?

Platform-aware (macOS / Windows / WSL): the terminal and agent checks adapt to
the detected OS. Each probe is injectable so the checks are unit-testable
without touching the real environment.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from . import action as action_mod
from . import launchd as launchd_mod
from . import osenv
from . import scheduler as scheduler_mod
from . import schedule
from .ccusage import CcusageUnavailable, get_active_block
from .config import Config

OK = "ok"
WARN = "warn"
FAIL = "fail"

ITERM_APP = "/Applications/iTerm.app"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _fmt(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _check_python() -> Check:
    return Check("python", OK, "Python %s" % _platform.python_version())


def _check_platform() -> Check:
    return Check("platform", OK, osenv.detect())


def _check_ccusage(cfg: Config, probe, now) -> Check:
    if cfg.at or cfg.every_hours:
        return Check("ccusage", OK, "not needed (fixed schedule configured)")
    try:
        block = probe(cfg.timeout)
    except CcusageUnavailable as e:
        return Check("ccusage", FAIL, "%s — auto-detect won't work (use --at/--every, or install Node+ccusage)" % e)
    if block is None:
        return Check("ccusage", WARN, "reachable, but no active window right now (idle)")
    mins = max(0, int((block.reset_at - now()).total_seconds() // 60))
    return Check("ccusage", OK, "active window resets %s (in %dh %02dm)" % (_fmt(block.reset_at), mins // 60, mins % 60))


def _check_node(cfg: Config, which) -> Check:
    node = which("node") or which("npx")
    if not node:
        if cfg.at or cfg.every_hours:
            return Check("node", WARN, "node/npx not found — only needed for ccusage auto-detect")
        return Check("node", FAIL, "node/npx not found on PATH — ccusage auto-detect won't work")
    # The version-pinned-PATH concern is launchd-specific (the plist bakes a PATH).
    # Task Scheduler inherits the live user env, so on Windows just report presence.
    if osenv.uses_launchd():
        if launchd_mod.stable_node_dir():
            return Check("node", OK, "%s (a stable node dir will be on the launchd PATH)" % node)
        if launchd_mod.is_volatile_node_dir(node):
            return Check("node", WARN, "%s is version-pinned — re-run `claude-continue install` after upgrading node" % node)
    return Check("node", OK, node)


def _check_agent(describe) -> Check:
    word, detail = describe()
    return Check("agent", OK if word == "running" else WARN, detail)


def _check_config(cfg: Config) -> Check:
    for label, value in (("--at", cfg.at), ("--anchor", cfg.anchor)):
        if value:
            try:
                schedule.parse_hhmm(value)
            except ValueError as e:
                return Check("config", FAIL, "%s invalid: %s" % (label, e))
    if cfg.exec_cmd:
        action = "exec"
    elif cfg.keystroke:
        action = "keystroke -> %r" % cfg.window_title
    elif cfg.session:
        action = "session %r" % cfg.session
    elif cfg.all_sessions:
        action = "all sessions"
    else:
        action = "filter %s" % cfg.filter
    if cfg.at:
        trigger = "fixed at %s" % cfg.at
    elif cfg.every_hours:
        trigger = "every %gh" % cfg.every_hours + (" from %s" % cfg.anchor if cfg.anchor else "")
    else:
        trigger = "ccusage auto"
    return Check("config", OK, "action=%s, trigger=%s, buffer=%ds" % (action, trigger, cfg.buffer))


def _check_action(cfg: Config, *, which, exists, preview) -> Check:
    # Headless exec: must parse and the binary must be resolvable.
    if cfg.exec_cmd:
        try:
            argv = osenv.split_command(cfg.exec_cmd)
        except ValueError as e:
            return Check("action", FAIL, "exec command does not parse: %s" % e)
        if not argv:
            return Check("action", FAIL, "exec command is empty")
        if not which(argv[0]):
            return Check("action", WARN, "exec binary %r not found on PATH (must also be on the agent's PATH)" % argv[0])
        return Check("action", OK, "headless: %s" % cfg.exec_cmd)

    plat = osenv.detect()
    # Capability check for the resume path.
    if plat == osenv.MACOS:
        if not exists(ITERM_APP):
            return Check("action", FAIL, "iTerm2 not found at %s — install it, or use --exec/--keystroke" % ITERM_APP)
    elif cfg.keystroke:
        if not (which("powershell.exe") or which("powershell") or which("pwsh")):
            return Check("action", FAIL, "--keystroke needs PowerShell, not found on PATH")
    else:
        return Check("action", WARN, "no resume action on %s — set --exec '<command>' or --keystroke" % plat)

    # Preview what would fire.
    try:
        out = preview()
    except action_mod.ActionError as e:
        return Check("action", WARN, str(e))
    except Exception as e:  # noqa: BLE001 - doctor must never raise
        return Check("action", WARN, "preview failed: %s — is the terminal running?" % e)
    if not out:
        return Check("action", WARN, "nothing to act on right now (filter %s, skip_busy=%s)" % (cfg.filter, cfg.skip_busy))
    return Check("action", OK, "%d target(s): %s" % (len(out), ", ".join(out)))


def run_checks(
    cfg: Config,
    *,
    which=shutil.which,
    iterm_exists=os.path.exists,
    ccusage_probe=get_active_block,
    scheduler_describe=None,
    action_preview=None,
    now=None,
) -> list:
    """Run every preflight check and return the ordered list of results."""
    now = now or (lambda: datetime.now(timezone.utc))
    scheduler_describe = scheduler_describe or scheduler_mod.describe
    # Delegate the preview to the real action layer so it stays identical to what
    # would actually fire (dry-run never sends keystrokes).
    action_preview = action_preview or (lambda: action_mod.perform(cfg, dry_run=True))

    return [
        _check_python(),
        _check_platform(),
        _check_ccusage(cfg, ccusage_probe, now),
        _check_node(cfg, which),
        _check_agent(scheduler_describe),
        _check_config(cfg),
        _check_action(cfg, which=which, exists=iterm_exists, preview=action_preview),
    ]


def worst_status(checks) -> str:
    statuses = {c.status for c in checks}
    if FAIL in statuses:
        return FAIL
    if WARN in statuses:
        return WARN
    return OK
