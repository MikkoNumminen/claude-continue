"""What to actually *do* at a reset.

Resolution order:
1. ``exec_cmd`` set  -> run it headless (cross-platform; the reliable default on
   Windows/WSL where there's no per-session "type into it" API).
2. ``tmux`` set      -> ``tmux send-keys`` into matching panes (terminal-agnostic:
   any terminal on macOS/Linux, as long as Claude runs inside tmux).
3. ``keystroke`` set -> type ``text`` into a terminal window
   (macOS: iTerm2 broadcast; Windows/WSL: PowerShell SendKeys).
4. otherwise         -> macOS broadcasts to iTerm2 (zero-config resume);
   Windows/WSL/Linux raise ActionError (set --exec, --tmux or --keystroke).

All failures surface as ``ActionError`` so the watch loop can degrade to
re-arm/poll instead of crashing the daemon.
"""

from __future__ import annotations

import subprocess

from . import iterm, osenv, tmux, winterm
from .config import Config


class ActionError(Exception):
    """The configured action could not be performed."""


def perform(cfg: Config, dry_run: bool = False) -> list:
    """Execute the configured action. Returns human-readable strings describing
    what was acted on (session names, the keystroke target, or the exec command)."""
    if cfg.exec_cmd:
        return _run_exec(cfg.exec_cmd, dry_run=dry_run)
    return _resume(cfg, dry_run=dry_run)


def _resume(cfg: Config, dry_run: bool) -> list:
    plat = osenv.detect()
    # tmux is terminal-agnostic and works on macOS/Linux alike — check it first so
    # a non-iTerm2 (or Linux) user can opt in regardless of platform.
    if cfg.tmux:
        return _broadcast_tmux(cfg, dry_run)
    # macOS resumes by broadcasting into iTerm2 (its keystroke equivalent).
    if plat == osenv.MACOS:
        return _broadcast_iterm(cfg, dry_run)
    # Windows/WSL: keystroke into a terminal window via PowerShell SendKeys.
    if cfg.keystroke and plat in (osenv.WINDOWS, osenv.WSL):
        try:
            return winterm.send_keystroke(
                cfg.text, window_title=cfg.window_title, dry_run=dry_run, timeout=float(cfg.timeout)
            )
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise ActionError("keystroke send failed: %s" % e) from e
    raise ActionError(
        "no resume action for this platform (%s) — set --exec '<command>' for a "
        "headless run, or --tmux to resume Claude panes running inside tmux%s"
        % (plat, ", or --keystroke" if plat in (osenv.WINDOWS, osenv.WSL) else "")
    )


def _broadcast_tmux(cfg: Config, dry_run: bool) -> list:
    try:
        return tmux.broadcast(
            cfg.text,
            cfg.filter,
            skip_busy=cfg.skip_busy,
            session=cfg.session,
            dry_run=dry_run,
            all_sessions=cfg.all_sessions,
            force=cfg.force,
            busy_pattern=cfg.tmux_busy_pattern,
            timeout=float(cfg.timeout),
        )
    except (tmux.TmuxError, OSError, subprocess.SubprocessError) as e:
        raise ActionError("tmux send failed: %s" % e) from e


def _broadcast_iterm(cfg: Config, dry_run: bool) -> list:
    try:
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
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        raise ActionError("iTerm2 broadcast failed: %s" % e) from e


def _run_exec(command: str, dry_run: bool = False) -> list:
    try:
        argv = osenv.split_command(command)
    except ValueError as e:
        raise ActionError("invalid exec command %r: %s" % (command, e)) from e
    if not argv:
        raise ActionError("exec command is empty")

    label = "exec: " + command
    if dry_run:
        return [label]
    # Detach so the headless run outlives this process and doesn't tie up the
    # watch loop. resolve_argv handles Windows .cmd shims (claude.cmd etc.).
    try:
        subprocess.Popen(
            osenv.resolve_argv(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **osenv.detached_popen_kwargs(),
        )
    except OSError as e:
        raise ActionError("failed to launch exec command %r: %s" % (command, e)) from e
    return [label]
