"""Preflight checks: will claude-continue actually work on this machine?

The tool depends on several external pieces that fail quietly (ccusage, node on
launchd's PATH, iTerm2, the loaded agent). ``doctor`` probes each and reports a
clear ok / warn / fail so problems surface *before* a window resets, not after.

Each probe is injectable so the checks are unit-testable without touching the
real environment.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from . import action as action_mod
from . import launchd as launchd_mod
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
    return Check("python", OK, "Python %s" % platform.python_version())


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
    stable_present = any(
        os.path.exists(os.path.join(d, "node")) for d in ("/opt/homebrew/bin", "/usr/local/bin")
    )
    if stable_present:
        return Check("node", OK, "%s (a stable node dir will be on the launchd PATH)" % node)
    if launchd_mod.is_volatile_node_dir(node):
        return Check("node", WARN, "%s is version-pinned — re-run `claude-continue install` after upgrading node" % node)
    return Check("node", OK, "%s (launchd PATH will include %s)" % (node, os.path.dirname(node)))


def _check_iterm(cfg: Config, exists) -> Check:
    if cfg.exec_cmd:
        return Check("iterm2", OK, "not needed (--exec headless mode)")
    if exists(ITERM_APP):
        return Check("iterm2", OK, "%s present" % ITERM_APP)
    return Check("iterm2", FAIL, "iTerm2 not found at %s — install it or use --exec" % ITERM_APP)


def _check_launchd(status_fn) -> Check:
    out = status_fn()
    if out.startswith("not loaded"):
        return Check("agent", WARN, "not installed (run `claude-continue install` to run unattended)")
    if "state = running" in out:
        return Check("agent", OK, "installed and running")
    return Check("agent", WARN, "installed but not running")


def _check_config(cfg: Config) -> Check:
    for label, value in (("--at", cfg.at), ("--anchor", cfg.anchor)):
        if value:
            try:
                schedule.parse_hhmm(value)
            except ValueError as e:
                return Check("config", FAIL, "%s invalid: %s" % (label, e))
    if cfg.exec_cmd:
        action = "exec"
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


def _check_action(cfg: Config, preview, which) -> Check:
    if cfg.exec_cmd:
        try:
            argv = shlex.split(cfg.exec_cmd)
        except ValueError as e:
            return Check("targets", FAIL, "exec command does not parse: %s" % e)
        if not argv:
            return Check("targets", FAIL, "exec command is empty")
        found = which(argv[0])
        if not found:
            return Check("targets", WARN, "exec binary %r not found on PATH (must be on launchd's PATH too)" % argv[0])
        return Check("targets", OK, "would run: %s (%s)" % (cfg.exec_cmd, found))
    try:
        names = preview()
    except Exception as e:  # noqa: BLE001 - doctor must never raise
        return Check("targets", WARN, "could not query iTerm2 (%s) — is it running?" % e)
    if not names:
        return Check("targets", WARN, "no sessions currently match (filter %s, skip_busy=%s)" % (cfg.filter, cfg.skip_busy))
    return Check("targets", OK, "%d session(s) currently match: %s" % (len(names), ", ".join(names)))


def run_checks(
    cfg: Config,
    *,
    which=shutil.which,
    iterm_exists=os.path.exists,
    ccusage_probe=get_active_block,
    launchd_status=None,
    action_preview=None,
    now=None,
) -> list:
    """Run every preflight check and return the ordered list of results."""
    now = now or (lambda: datetime.now(timezone.utc))
    launchd_status = launchd_status or launchd_mod.status
    # Delegate the preview to the real action layer so it stays identical to what
    # would actually fire (dry-run never sends keystrokes).
    action_preview = action_preview or (lambda: action_mod.perform(cfg, dry_run=True))

    return [
        _check_python(),
        _check_ccusage(cfg, ccusage_probe, now),
        _check_node(cfg, which),
        _check_iterm(cfg, iterm_exists),
        _check_launchd(launchd_status),
        _check_config(cfg),
        _check_action(cfg, action_preview, which),
    ]


def worst_status(checks) -> str:
    statuses = {c.status for c in checks}
    if FAIL in statuses:
        return FAIL
    if WARN in statuses:
        return WARN
    return OK
