"""Command-line interface: status | watch | once | fire | install | uninstall."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, action, doctor, osenv, schedule, scheduler, watch
from .ccusage import CcusageUnavailable, get_active_block
from .config import Config, resolve
from .lock import AlreadyRunning
from .log import get_logger

CONFIG_FIELDS = [f.name for f in fields(Config)]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _csv(s: str):
    return [p.strip() for p in s.split(",") if p.strip()]


# --- argument wiring ---------------------------------------------------------

def add_action_args(p: argparse.ArgumentParser, *, dry_run: bool = False) -> None:
    """Add the action + timing flags shared by watch/once/fire/install/status.

    All defaults are ``None`` so that "flag not given" never clobbers a value
    from env or the config file (see config.resolve precedence).
    """
    a = p.add_argument_group("action")
    a.add_argument("--text", default=None, help="text to send (default: continue)")
    a.add_argument("--exec", dest="exec_cmd", default=None, metavar="CMD",
                   help="run this headless command instead of broadcasting to iTerm2")
    a.add_argument("--session", default=None, metavar="NAME",
                   help="target a single session whose name contains NAME")
    a.add_argument("--all", dest="all_sessions", action="store_true", default=None,
                   help="match all sessions (drop the name filter)")
    a.add_argument("--force", action="store_true", default=None,
                   help="with --all, also disable skip-busy")
    a.add_argument("--skip-busy", dest="skip_busy", action="store_true", default=None,
                   help="skip sessions that are mid-turn (default: on)")
    a.add_argument("--no-skip-busy", dest="skip_busy", action="store_false", default=None,
                   help="do not skip busy sessions")
    a.add_argument("--filter", dest="filter", default=None, type=_csv, metavar="A,B",
                   help="comma-separated session-name substrings to match")
    a.add_argument("--keystroke", dest="keystroke", action="store_true", default=None,
                   help="Windows/WSL: type the text into a terminal window (opt-in, best-effort)")
    a.add_argument("--window-title", dest="window_title", default=None, metavar="TITLE",
                   help="window title to target in --keystroke mode (default: Windows Terminal)")
    a.add_argument("--tmux", dest="tmux", action="store_true", default=None,
                   help="resume Claude panes running inside tmux (any terminal, macOS/Linux)")
    a.add_argument("--tmux-busy-pattern", dest="tmux_busy_pattern", default=None, metavar="TEXT",
                   help="pane content marking a busy session in --tmux mode (default: 'esc to interrupt')")
    a.add_argument("--start-window", dest="start_window", action="store_true", default=None,
                   help="quota mode: open a fresh usage window headlessly instead of resuming terminals")
    a.add_argument("--window-cmd", dest="window_cmd", default=None, metavar="CMD",
                   help="headless command that opens a window in --start-window mode (default: a tiny `claude -p`)")

    t = p.add_argument_group("timing")
    t.add_argument("--buffer", type=int, default=None, metavar="S",
                   help="seconds after reset before firing (default: 90)")
    t.add_argument("--verify-delay", dest="verify_delay", type=int, default=None, metavar="S")
    t.add_argument("--poll-interval", dest="poll_interval", type=int, default=None, metavar="S")
    t.add_argument("--retry-interval", dest="retry_interval", type=int, default=None, metavar="S")
    t.add_argument("--retry-cap", dest="retry_cap", type=int, default=None, metavar="N")
    t.add_argument("--timeout", type=int, default=None, metavar="S",
                   help="ccusage subprocess timeout (default: 30)")
    t.add_argument("--at", default=None, metavar="HH:MM",
                   help="fixed fire time (fixed-schedule mode, no ccusage)")
    t.add_argument("--every", dest="every_hours", type=float, default=None, metavar="H",
                   help="fire every H hours (fixed-schedule mode)")
    t.add_argument("--anchor", default=None, metavar="HH:MM", help="anchor time for --every")

    if dry_run:
        p.add_argument("--dry-run", action="store_true", help="show what would happen; do nothing")


def build_overrides(args: argparse.Namespace) -> dict:
    return {name: getattr(args, name) for name in CONFIG_FIELDS if hasattr(args, name)}


def overrides_to_argv(overrides: dict) -> list:
    """Reconstruct the explicit flags so `install` can bake them into the plist."""
    argv = []
    for name, value in overrides.items():
        if value is None:
            continue
        if name == "skip_busy":
            argv.append("--skip-busy" if value else "--no-skip-busy")
        elif name == "all_sessions":
            if value:
                argv.append("--all")
        elif name == "force":
            if value:
                argv.append("--force")
        elif name == "keystroke":
            if value:
                argv.append("--keystroke")
        elif name == "tmux":
            if value:
                argv.append("--tmux")
        elif name == "start_window":
            if value:
                argv.append("--start-window")
        elif name == "exec_cmd":
            argv += ["--exec", str(value)]
        elif name == "every_hours":
            argv += ["--every", str(value)]
        elif name == "filter":
            argv += ["--filter", ",".join(value)]
        elif name in ("node_path", "log_path"):
            continue  # launchd-only, not watch flags
        else:
            argv += ["--" + name.replace("_", "-"), str(value)]
    return argv


def _launch_argv() -> list:
    """How a scheduler should launch us: prefer the installed console script,
    then the repo shim (POSIX), else run the module with this interpreter."""
    found = shutil.which("claude-continue")
    if found:
        return [found]
    shim = Path(__file__).resolve().parents[2] / "bin" / "claude-continue"
    if shim.exists():
        # On Windows the shim has no executable shebang, so drive it via the
        # interpreter; on POSIX the shebang makes it directly runnable.
        return [sys.executable, str(shim)] if os.name == "nt" else [str(shim)]
    return [sys.executable, "-m", "claude_continue.cli"]


# --- subcommands -------------------------------------------------------------
#
# Output convention: state/setup commands (status, doctor, install, uninstall)
# print to stdout; action-performing commands (watch, once, fire) use the logger
# so each line is timestamped — it matters *when* something fired, and these are
# the commands that may run unattended / under cron / under launchd.

def cmd_status(args) -> int:
    cfg = resolve(build_overrides(args))
    try:
        block = get_active_block(cfg.timeout)
    except CcusageUnavailable as e:
        print("ccusage unavailable: %s" % e)
        print("  (install Node + ccusage, or run with --at/--every for a fixed schedule)")
        block = None

    if block is None:
        print("No active usage window (idle) — the next window starts on your next message.")
    else:
        now = _utc_now()
        mins = max(0, int((block.reset_at - now).total_seconds() // 60))
        print("Active window:")
        print("  started: %s" % _fmt(block.start))
        print("  resets:  %s  (in %dh %02dm)" % (_fmt(block.reset_at), mins // 60, mins % 60))
        print("  fire at: %s  (reset + %ds buffer)" % (_fmt(schedule.next_target(block, cfg.buffer)), cfg.buffer))

    if cfg.exec_cmd:
        print("Action: exec -> %s" % cfg.exec_cmd)
    else:
        try:
            targets = action.perform(cfg, dry_run=True)
            print("Action: send %r to %d session(s):" % (cfg.text, len(targets)))
            for name in targets:
                print("  - %s" % name)
        except Exception as e:  # noqa: BLE001 - status must never raise
            print("Action preview failed: %s" % e)
    return 0


def cmd_doctor(args) -> int:
    cfg = resolve(build_overrides(args))
    checks = doctor.run_checks(cfg)
    symbol = {doctor.OK: "✓", doctor.WARN: "!", doctor.FAIL: "✗"}
    for c in checks:
        print("%s %-9s %s" % (symbol[c.status], c.name, c.detail))
    worst = doctor.worst_status(checks)
    print("")
    if worst == doctor.FAIL:
        print("Some checks FAILED — fix the above before relying on the agent.")
        return 1
    print("Ready, with warnings." if worst == doctor.WARN else "All checks passed.")
    return 0


def cmd_gui(args) -> int:
    from . import gui  # gui module is import-safe; tkinter is imported inside run()
    try:
        gui.run()
    except ImportError as e:
        print("GUI unavailable — tkinter is not installed: %s" % e)
        print("  (install Python's Tk support, or use `claude-continue watch`)")
        return 1
    return 0


def cmd_watch(args) -> int:
    cfg = resolve(build_overrides(args))
    logger = get_logger()
    try:
        watch.run(cfg, logger=logger)
    except AlreadyRunning as e:
        logger.error(str(e))
        return 1
    return 0


def cmd_once(args) -> int:
    cfg = resolve(build_overrides(args))
    logger = get_logger()
    now = _utc_now()

    if cfg.at or cfg.every_hours:
        target = schedule.fixed_target(now, at=cfg.at, every_hours=cfg.every_hours, anchor=cfg.anchor)
    else:
        try:
            block = get_active_block(cfg.timeout)
        except CcusageUnavailable as e:
            logger.error("ccusage unavailable and no --at/--every given: %s", e)
            return 1
        if block is None:
            logger.error("no active window to wait for; pass --at HH:MM for a fixed time")
            return 1
        target = schedule.next_target(block, cfg.buffer)

    try:
        if getattr(args, "dry_run", False):
            logger.info("would wait until %s, then fire -> %s", _fmt(target), action.perform(cfg, dry_run=True))
            return 0
        logger.info("waiting until %s ...", _fmt(target))
        watch._sleep_until(target, clock=_utc_now, sleep=time.sleep, stop=lambda: False)
        fired = action.perform(cfg, dry_run=False)
    except action.ActionError as e:
        logger.error("%s", e)
        return 1
    logger.info("fired -> %s", fired or "(no matching sessions)")
    return 0


def cmd_fire(args) -> int:
    cfg = resolve(build_overrides(args))
    logger = get_logger()
    dry = bool(getattr(args, "dry_run", False))
    try:
        fired = action.perform(cfg, dry_run=dry)
    except action.ActionError as e:
        logger.error("%s", e)
        return 1
    logger.info("%s -> %s", "would fire" if dry else "fired", fired or "(no matching sessions)")
    return 0


def cmd_install(args) -> int:
    overrides = build_overrides(args)
    cfg = resolve(overrides)
    launch_argv = _launch_argv()
    watch_flags = overrides_to_argv(overrides)
    try:
        lines = scheduler.install(launch_argv, watch_flags, cfg)
    except (RuntimeError, ValueError) as e:
        print("install failed: %s" % e)
        return 1
    for line in lines:
        print(line)
    return 0


def cmd_uninstall(args) -> int:
    if getattr(args, "app", False):
        from . import selfremove
        summary = selfremove.remove(purge_config=True, logger=lambda *a: print(a[0] % a[1:] if len(a) > 1 else a[0]))
        print("removed the unattended agent" if summary["agent_removed"] else "no unattended agent was installed")
        for p in summary["deleted"]:
            print("deleted %s" % p)
        if summary["bundle"] and summary["bundle_scheduled"]:
            print("the app will delete itself (%s) once this process exits." % summary["bundle"])
        elif summary["frozen"]:
            where = " (%s)" % summary["bundle"] if summary["bundle"] else ""
            print("couldn't delete the app bundle%s — please remove it manually." % where)
        else:
            print("running from source — nothing to self-delete (remove the repo, or `pip uninstall claude-continue`).")
        return 0
    existed = scheduler.uninstall(purge=bool(args.purge))
    if existed:
        # --purge only removes a file on macOS (the plist); the Windows task has none.
        suffix = " (plist removed)" if (args.purge and osenv.uses_launchd()) else ""
        print("uninstalled the unattended agent%s" % suffix)
    else:
        print("nothing to uninstall (no agent/task found)")
    return 0


def cmd_update(args) -> int:
    from . import update
    info = update.check()
    if info.error:
        print("update check failed: %s" % info.error)
        return 1
    if not info.newer:
        print("up to date (v%s)" % info.current)
        return 0
    print("%s is available (you have v%s)." % (info.latest, info.current))
    if not update.is_frozen():
        print("  running from source — update with `git pull` (or pip), not here.")
        return 0
    if not info.asset_url:
        print("  no downloadable build for this platform — see %s" % update.RELEASES_PAGE)
        return 0
    if not args.apply:
        print("  run `claude-continue update --apply` to download and replace this build.")
        return 0
    try:
        print("downloading %s…" % info.asset_name)
        target = update.apply_update(info, relaunch=False)
        print("installed %s -> %s" % (info.latest, target))
    except update.UpdateError as e:
        print("update failed: %s" % e)
        return 1
    return 0


# --- entrypoint --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-continue",
        description="Keep Claude Code's 5-hour usage windows running back-to-back: "
        "the instant a window resets, resume your paused sessions.",
    )
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True  # Python 3.9 needs this set explicitly

    p_status = sub.add_parser("status", help="show the active window + what would fire")
    add_action_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="preflight: check ccusage, node, the resume action, the agent, config")
    add_action_args(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_watch = sub.add_parser("watch", help="run the self-rescheduling loop (foreground)")
    add_action_args(p_watch)
    p_watch.set_defaults(func=cmd_watch)

    p_gui = sub.add_parser("gui", help="open a one-button toggle window (Tkinter)")
    p_gui.set_defaults(func=cmd_gui)

    p_once = sub.add_parser("once", help="wait for the next reset, fire once, exit")
    add_action_args(p_once, dry_run=True)
    p_once.set_defaults(func=cmd_once)

    p_fire = sub.add_parser("fire", help="fire the action immediately")
    add_action_args(p_fire, dry_run=True)
    p_fire.set_defaults(func=cmd_fire)

    p_install = sub.add_parser("install", help="install the unattended agent (launchd on macOS, Task Scheduler on Windows) to run `watch`")
    add_action_args(p_install)
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="remove the unattended agent (launchd / Task Scheduler)")
    p_uninstall.add_argument("--purge", action="store_true", help="also delete the launchd plist file (macOS only)")
    p_uninstall.add_argument("--app", action="store_true",
                             help="remove EVERYTHING: agent, settings, logs, and the app/exe itself")
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_update = sub.add_parser("update", help="check for a newer release (--apply to download + replace this build)")
    p_update.add_argument("--apply", action="store_true", help="download and replace the running build if newer")
    p_update.set_defaults(func=cmd_update)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
