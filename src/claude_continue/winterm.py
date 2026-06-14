"""Best-effort keystroke injection into a Windows terminal window.

The Windows/WSL analogue of the macOS iTerm2 broadcast. There is no reliable,
per-tab "type into a session" API on Windows, so this activates a window by
title and uses ``WScript.Shell.SendKeys`` via PowerShell (built in, no extra
deps; reachable from WSL through ``powershell.exe`` interop).

Fragile by nature — it needs the target window present and steals focus while
sending. It is opt-in (``--keystroke``); the headless ``--exec`` path is the
reliable default on Windows.
"""

from __future__ import annotations

import shutil
import subprocess

DEFAULT_WINDOW_TITLE = "Windows Terminal"

# SendKeys treats these as metacharacters; each must be wrapped in braces to be literal.
_SENDKEYS_META = set("+^%~(){}[]")


def _escape_sendkeys(text: str) -> str:
    return "".join("{%s}" % c if c in _SENDKEYS_META else c for c in text)


def _ps_quote(s: str) -> str:
    # single-quoted PowerShell string: double any embedded single quotes
    return s.replace("'", "''")


def build_script(text: str, window_title: str) -> str:
    """The PowerShell one-liner that focuses the window and types ``text``+Enter."""
    keys = _escape_sendkeys(text) + "{ENTER}"
    return (
        "$ErrorActionPreference='Stop'; "
        "$w = New-Object -ComObject WScript.Shell; "
        "if (-not $w.AppActivate('%s')) { Write-Error 'window not found: %s'; exit 3 }; "
        "Start-Sleep -Milliseconds 250; "
        "$w.SendKeys('%s')"
        % (_ps_quote(window_title), _ps_quote(window_title), _ps_quote(keys))
    )


def _powershell_bin() -> str:
    # native Windows: powershell.exe / pwsh; WSL: powershell.exe via interop
    for name in ("powershell.exe", "pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return "powershell.exe"


def send_keystroke(text: str, *, window_title: str = DEFAULT_WINDOW_TITLE,
                   dry_run: bool = False, timeout: float = 30.0) -> list:
    """Type ``text`` (plus Enter) into the window whose title contains
    ``window_title``. Returns a one-element description list."""
    label = "keystroke %r -> window %r" % (text, window_title)
    if dry_run:
        return [label]
    script = build_script(text, window_title)
    try:
        proc = subprocess.run(
            [_powershell_bin(), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError("powershell not found: %s" % e) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("powershell SendKeys timed out after %ss" % timeout) from e
    except OSError as e:
        raise RuntimeError("failed to run powershell: %s" % e) from e
    if proc.returncode != 0:
        raise RuntimeError("SendKeys failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip()))
    return [label]
