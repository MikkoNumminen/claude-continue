"""Remove claude-continue completely: the unattended agent, the config + logs,
and — for the frozen .app/.exe — the running bundle itself.

Deleting the bundle is the tricky part: we can't remove a file we're executing
from, so (exactly like the self-update) we spawn a DETACHED helper that waits for
this process to exit, then deletes the bundle and itself. Config/logs aren't in
use, so they're deleted inline before the helper is launched.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import osenv, scheduler, update
from .config import CONFIG_PATH
from .launchd import ERR_LOG_PATH, LOG_PATH


def leftover_paths() -> list:
    """User-data paths a complete removal deletes (besides the agent + bundle)."""
    return [str(CONFIG_PATH.parent), str(LOG_PATH), str(ERR_LOG_PATH)]


def removal_target() -> str | None:
    """The frozen bundle/exe to self-delete, or None when running from source."""
    if not update.is_frozen():
        return None
    if osenv.is_macos():
        return update.macos_bundle_path()  # the .app bundle
    return os.path.realpath(sys.executable)  # the .exe


def macos_self_delete_script(target: str, pid: int) -> str:
    """Detached helper: wait for our PID to exit, then rm -rf the bundle + itself."""
    return (
        "#!/bin/sh\n"
        "while kill -0 %d 2>/dev/null; do sleep 0.3; done\n" % pid
        + "rm -rf %s\n" % shlex.quote(target)
        + 'rm -f "$0"\n'
    )


def windows_self_delete_script(target: str, *, wait_s: int = 5) -> str:
    """Detached .cmd: wait for our process to exit, then delete the exe + itself.

    Runs console-less (CREATE_NO_WINDOW), where ``ping``/``timeout`` don't delay
    and a ``tasklist | find`` PID-poll pipe won't connect — so it uses a fixed
    ``waitfor /t`` sleep (the same fix as the self-update swap). The exe stays
    locked until the PyInstaller bootstrap + child both exit, so the delete gets a
    second attempt after another wait."""
    return "\r\n".join([
        "@echo off",
        "waitfor /t %d ClaudeContinueRemove 2>NUL" % wait_s,
        'del /F /Q "%s" >NUL 2>&1' % target,
        'if exist "%s" (waitfor /t %d ClaudeContinueRemove2 2>NUL & del /F /Q "%s" >NUL 2>&1)' % (target, wait_s, target),
        'del "%~f0"',
    ]) + "\r\n"


def _spawn_self_delete(target: str) -> None:
    if osenv.is_macos():
        script = macos_self_delete_script(target, os.getpid())
        path = os.path.join(tempfile.gettempdir(), "claude-continue-remove.sh")
        with open(path, "w") as f:
            f.write(script)
        os.chmod(path, 0o755)
        subprocess.Popen(["/bin/sh", path], **osenv.detached_popen_kwargs())
    else:
        script = windows_self_delete_script(target)
        path = os.path.join(tempfile.gettempdir(), "claude-continue-remove.cmd")
        # newline="" so \r\n isn't doubled; CREATE_NO_WINDOW (not DETACHED) so the
        # waitfor-based script runs correctly and no console flashes.
        with open(path, "w", newline="") as f:
            f.write(script)
        subprocess.Popen(["cmd", "/c", path], **osenv.no_window_kwargs())


def remove(*, purge_config: bool = True, logger=None) -> dict:
    """Tear claude-continue down. Removes the unattended agent always; deletes
    config + logs when ``purge_config``; and, if running as a frozen bundle,
    spawns a detached helper to delete the bundle once we exit.

    Returns a summary. The caller should exit promptly so the helper can run.
    Never raises — a complete removal should degrade, not stall half-done.
    """
    log = logger or (lambda *a: None)
    summary: dict = {"agent_removed": False, "deleted": [], "bundle": None,
                     "bundle_scheduled": False, "frozen": update.is_frozen()}

    try:
        summary["agent_removed"] = bool(scheduler.uninstall(purge=True))
    except Exception as e:  # noqa: BLE001 - keep removing even if the agent step fails
        log("agent removal failed: %s", e)

    if purge_config:
        for path in leftover_paths():
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    summary["deleted"].append(path)
                elif os.path.exists(path):
                    os.remove(path)
                    summary["deleted"].append(path)
            except OSError as e:
                log("couldn't delete %s: %s", path, e)

    target = removal_target()
    summary["bundle"] = target
    if target:
        try:
            _spawn_self_delete(target)
            summary["bundle_scheduled"] = True  # only true if the helper actually launched
        except (OSError, subprocess.SubprocessError) as e:
            log("couldn't schedule bundle deletion: %s", e)
    return summary
