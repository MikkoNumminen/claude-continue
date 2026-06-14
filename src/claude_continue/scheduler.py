"""Platform-dispatched unattended scheduling: launchd on macOS, Task Scheduler
on Windows/WSL. Keeps cli/doctor free of per-OS branching."""

from __future__ import annotations

import shutil

from . import launchd, osenv, tasksched


def _unsupported() -> RuntimeError:
    return RuntimeError(
        "unattended scheduling isn't supported on %s — run `claude-continue watch` "
        "under your own service manager (e.g. systemd)" % osenv.detect()
    )


def install(launch_argv, watch_flags, cfg) -> list:
    """Register the watch loop to run unattended. Returns lines to print.

    ``launch_argv`` is how to invoke this tool (e.g. ['claude-continue'] or
    [python, '-m', 'claude_continue.cli']); 'watch' + flags are appended.
    """
    if not (osenv.uses_launchd() or osenv.uses_task_scheduler()):
        raise _unsupported()
    if osenv.uses_launchd():
        program_args = list(launch_argv) + ["watch"] + list(watch_flags)
        path_value = launchd.node_path_value(cfg.node_path)
        plist = launchd.install(program_args, path_value=path_value, stdout=cfg.log_path)
        lines = [
            "installed launchd agent: %s" % plist,
            "  program: %s" % " ".join(program_args),
            "  logs:    %s" % (cfg.log_path or launchd.LOG_PATH),
            "  check:   launchctl print %s" % launchd._service(),
        ]
        node = shutil.which("node") or shutil.which("npx")
        if node and launchd.is_volatile_node_dir(node) and not launchd.stable_node_dir():
            lines.append("  note:    node is version-pinned; re-run `claude-continue install` after upgrading node")
        return lines

    tr = tasksched.install(launch_argv, watch_flags, cfg)
    return [
        "installed scheduled task: %s (runs at logon)" % tasksched.TASK_NAME,
        "  runs:  %s" % tr,
        "  check: schtasks /query /tn %s" % tasksched.TASK_NAME,
    ]


def uninstall(purge=False) -> bool:
    if osenv.uses_launchd():
        return launchd.uninstall(purge=purge)
    if osenv.uses_task_scheduler():
        return tasksched.uninstall(purge=purge)
    return False


def describe():
    """Return (state_word, detail), state_word ∈ {absent, running, installed}."""
    if osenv.uses_launchd():
        out = launchd.status()
        if out.startswith("not loaded"):
            return ("absent", "not installed (run `claude-continue install` to run unattended)")
        if "state = running" in out:
            return ("running", "launchd agent installed and running")
        return ("installed", "launchd agent installed but not running")
    if osenv.uses_task_scheduler():
        return tasksched.describe()
    return ("absent", "unattended scheduling not supported on %s" % osenv.detect())
