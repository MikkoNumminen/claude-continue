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
import tempfile

from . import osenv, scheduler, update
from .config import CONFIG_PATH
from .launchd import ERR_LOG_PATH, LOG_PATH


def leftover_paths() -> list:
    """User-data paths a complete removal deletes (besides the agent + bundle)."""
    return [str(CONFIG_PATH.parent), str(LOG_PATH), str(ERR_LOG_PATH)]


def removal_target() -> str | None:
    """The frozen bundle/dir to self-delete, or None when running from source."""
    if not update.is_frozen():
        return None
    if osenv.is_macos():
        return update.macos_bundle_path()  # the .app bundle
    return update._install_dir()  # the one-dir install folder (exe + _internal\)


def macos_self_delete_script(target: str, pid: int) -> str:
    """Detached helper: wait (capped) for our PID to exit, then rm -rf the bundle + itself.

    The wait is bounded (~30s) like the Windows helper: ``kill -0`` only proves *a*
    process holds the PID, so a recycled or never-dying PID would otherwise hang the
    helper forever. After the cap it proceeds anyway."""
    return (
        "#!/bin/sh\n"
        "i=0\n"
        "while kill -0 %d 2>/dev/null && [ $i -lt 100 ]; do sleep 0.3; i=$((i+1)); done\n" % pid
        + "rm -rf %s\n" % shlex.quote(target)
        + 'rm -f "$0"\n'
    )


def windows_self_delete_script(target: str, *, pid: int, wait_s: int = 30) -> str:
    """Detached .cmd: wait for our process to exit, then delete the install DIR + itself.

    ``target`` is the one-dir install folder (the exe + its ``_internal\\``), so the
    whole tree is removed with ``rmdir /S /Q``. The helper itself lives in %TEMP%
    (outside the install dir), so deleting the dir can't kill the running helper.

    Runs console-less (CREATE_NO_WINDOW). Polls for our ``pid`` to exit (via file
    redirection — pipes don't connect window-less), capped by a counter so it can
    never hang. ``waitfor`` supplies each per-iteration delay (it blocks window-less,
    unlike timeout/ping). A failed/absent ``tasklist`` (checked via errorlevel) is
    treated as "can't confirm exit" and keeps waiting, never an immediate delete.
    The tree may stay locked until the PyInstaller bootstrap exits, so the delete
    gets a second attempt after another short wait.

    NOTE (flagged): unit-tested for text only, not run on real Windows — mirrors
    the self-update swap helper; verify before relying on it. The caller guards
    ``target`` with the same path-safety check the swap uses."""
    wait_file = "%TEMP%\\cc-remove-wait.txt"
    return "\r\n".join([
        "@echo off",
        # don't hold the install dir as our CWD — Windows blocks rmdir of a live
        # process's current directory (Popen also sets cwd; this is belt-and-suspenders).
        'cd /d "%TEMP%"',
        "set _i=0",
        ":ccwait",
        'tasklist /FI "PID eq %d" /NH > "%s" 2>NUL' % (pid, wait_file),
        "if errorlevel 1 goto cctick",
        'findstr /C:"%d" "%s" >NUL || goto ccgone' % (pid, wait_file),
        ":cctick",
        "set /a _i+=1",
        "if %%_i%% GEQ %d goto ccgone" % wait_s,
        "waitfor /t 1 ClaudeContinueRemovePoll >NUL 2>&1",
        "goto ccwait",
        ":ccgone",
        'del "%s" >NUL 2>&1' % wait_file,
        'rmdir /S /Q "%s" >NUL 2>&1' % target,
        # one more attempt after a short delay if the bootstrap still held the tree.
        'if exist "%s" (waitfor /t 2 ClaudeContinueRemove2 >NUL 2>&1 & rmdir /S /Q "%s" >NUL 2>&1)' % (target, target),
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
        # Same guard the swap helper uses: a '%' (or other cmd-unsafe char) is legal
        # in a Windows path but would corrupt the emitted .cmd. Refuse rather than
        # spawn a malformed helper that could `del` an unintended path.
        update._assert_swap_safe_path(target, "the bundle path")
        script = windows_self_delete_script(target, pid=os.getpid())
        path = os.path.join(tempfile.gettempdir(), "claude-continue-remove.cmd")
        # newline="" so \r\n isn't doubled; CREATE_NO_WINDOW (not DETACHED) so the
        # waitfor-based script runs correctly and no console flashes.
        with open(path, "w", newline="") as f:
            f.write(script)
        # cwd=%TEMP%: the helper must NOT inherit our install-dir CWD, or Windows
        # would block the rmdir of that very directory (it's a live CWD).
        subprocess.Popen(["cmd", "/c", path], cwd=tempfile.gettempdir(),
                         **osenv.no_window_kwargs())


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

    # Drop the Start Menu shortcut + App Paths key the GUI registered (Windows only;
    # best-effort) so a complete removal doesn't leave an orphaned shortcut behind.
    try:
        from . import winshortcut
        winshortcut.unregister()
    except Exception as e:  # noqa: BLE001 - never stall teardown over a shortcut
        log("couldn't remove the Start Menu shortcut: %s", e)

    target = removal_target()
    summary["bundle"] = target
    if target:
        try:
            _spawn_self_delete(target)
            summary["bundle_scheduled"] = True  # only true if the helper actually launched
        except (OSError, subprocess.SubprocessError, update.UpdateError) as e:
            # UpdateError = the bundle path is cmd-unsafe (e.g. has a '%'); leave the
            # bundle in place rather than emit a corrupt delete helper.
            log("couldn't schedule bundle deletion: %s", e)
    return summary
