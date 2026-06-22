"""Register the frozen Windows build in the Start Menu so Windows Search finds it,
and keep it pointing at the (stable) install path across one-dir self-updates.

Windows-only, frozen-build-only, and entirely best-effort: a missing or failed
shortcut must NEVER wedge the GUI, so every entry point swallows its own errors.

Two mechanisms, complementary:
  * a Start Menu ``.lnk`` — this is what the search bar surfaces when you type
    "claude-continue". Created via ``WScript.Shell`` (no extra dependency; the same
    COM helper Explorer uses), only when missing or pointing at the wrong exe.
  * an ``App Paths`` registry key (stdlib ``winreg``) — makes Win+R ``claude-continue``
    launch it too, and doubles as the "where is it currently registered" marker so
    we can detect a moved install and re-register without re-spawning anything.

Because the one-dir self-update swaps the install folder's *contents* but keeps the
same path (``<install>\\claude-continue.exe``), a shortcut to that fixed path stays
correct after every update — no per-update maintenance.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import osenv, update

_APP = "claude-continue"
_EXE = "claude-continue.exe"
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\claude-continue.exe"
# ASCII only: the description rides through subprocess argv into PowerShell, and this
# codebase has known cp1252 trouble with non-ASCII on Windows — a hyphen avoids it.
_DESCRIPTION = "claude-continue - keep Claude Code 5-hour usage windows back-to-back"


def _enabled() -> bool:
    """Only the frozen Windows .exe should register itself — never a from-source run
    (which would point a shortcut at python.exe) or another OS."""
    return update.is_frozen() and osenv.is_windows()


def start_menu_lnk_path() -> str:
    """The per-user Start Menu shortcut path Windows Search indexes."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", _APP + ".lnk")


def _powershell() -> str:
    return shutil.which("powershell") or shutil.which("powershell.exe") or "powershell.exe"


def powershell_create_shortcut_script(lnk: str, target: str, workdir: str) -> str:
    """A one-shot PowerShell command that (re)creates the .lnk via WScript.Shell.

    All three paths are app-controlled (derived from sys.executable / %APPDATA%),
    never user input — but we still single-quote and double any embedded quote so a
    path can't break out of the string literal."""
    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"
    return (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);" % q(lnk)
        + "$s.TargetPath=%s;" % q(target)
        + "$s.WorkingDirectory=%s;" % q(workdir)
        + "$s.Description=%s;" % q(_DESCRIPTION)
        + "$s.Save()"
    )


def _registered_target() -> str | None:
    """The exe path our App Paths key currently points at, or None if unset/absent.
    Doubles as the 'already registered here' marker so ensure_registered is a no-op
    when nothing moved."""
    import winreg  # Windows-only stdlib; only reached when _enabled()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APP_PATHS_KEY) as k:
            val, _ = winreg.QueryValueEx(k, "")  # "" = the key's default value
            return val
    except OSError:
        return None


def _set_app_paths(target: str, workdir: str) -> None:
    import winreg  # Windows-only stdlib; only reached when _enabled()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _APP_PATHS_KEY) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, target)  # "" = default value
        winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, workdir)


def ensure_registered(target: str | None = None) -> None:
    """Best-effort: make the Start Menu search bar find THIS build, and keep it
    current. No-op off the frozen Windows app, a no-op when already registered at
    the current path, and self-healing when the install moved. Never raises."""
    if not _enabled():
        return
    try:
        target = target or os.path.realpath(sys.executable)
        workdir = os.path.dirname(target)
        lnk = start_menu_lnk_path()
        try:
            current = _registered_target()
        except Exception:  # noqa: BLE001 - winreg read is advisory; fall through to re-register
            current = None
        if current == target and os.path.exists(lnk):
            return  # already registered and pointing at the live exe — nothing to do
        try:
            _set_app_paths(target, workdir)
        except Exception:  # noqa: BLE001 - registry is the bonus path; the .lnk still matters
            pass
        try:
            os.makedirs(os.path.dirname(lnk), exist_ok=True)
            script = powershell_create_shortcut_script(lnk, target, workdir)
            # Detached + no console window: fire-and-forget so GUI startup never waits
            # on it; a failure just means the next launch retries (lnk still missing).
            subprocess.Popen(
                [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
                **osenv.no_window_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    except Exception:  # noqa: BLE001 - belt-and-suspenders; must never wedge the GUI
        pass


def unregister() -> None:
    """Best-effort removal of the Start Menu shortcut + App Paths key, for a complete
    uninstall. Windows-only; never raises."""
    if not osenv.is_windows():
        return
    try:
        lnk = start_menu_lnk_path()
        if os.path.exists(lnk):
            os.remove(lnk)
    except OSError:
        pass
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _APP_PATHS_KEY)
    except Exception:  # noqa: BLE001 - key absent (already clean) or winreg unavailable
        pass
