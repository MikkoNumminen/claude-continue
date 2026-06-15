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


# --- window listing (the GUI's "Claude instances" panel on Windows) ----------
#
# Windows has no per-session "is processing" API (the iTerm2 panel's basis), so
# the honest analogue is: which top-level terminal windows can I see, and which
# one will the keystroke land in? ``AppActivate`` matches a window whose title
# *contains* the target string, so we mirror that here — the panel then shows the
# user whether their ``--window-title`` target is actually present.

# A title-listing one-liner (built into Windows; reachable from WSL via interop).
# MainWindowTitle is the visible top-level window title per process; blanks are
# dropped (background services), and Sort -Unique de-dupes identical titles.
_LIST_SCRIPT = (
    "Get-Process | Where-Object { $_.MainWindowTitle } | "
    "ForEach-Object { $_.MainWindowTitle } | Sort-Object -Unique"
)


def build_list_script() -> str:
    return _LIST_SCRIPT


def _parse_titles(stdout: str) -> list:
    return [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]


def select_windows(titles, name_filter, window_title: str, *, exclude=()) -> list:
    """Classify visible window titles for the keystroke panel. Pure/testable.

    Returns ``[(title, status)]`` where status is:
      - "target": the title contains ``window_title`` — where a keystroke would
        actually land (matching ``WScript.Shell.AppActivate``'s substring match);
      - "match":  the title contains one of the ``name_filter`` terms (a likely
        Claude terminal) but is not the keystroke target.

    Titles in ``exclude`` are dropped (case-insensitive, exact) — the GUI passes
    its own window title so the app doesn't list itself as a candidate terminal.
    Targets come first; titles matching neither are dropped. Match is
    case-insensitive (AppActivate is too), de-duplicated, otherwise order-stable.
    """
    target_key = (window_title or "").lower()
    terms = [t.lower() for t in (name_filter or []) if t]
    skip = {x.lower() for x in exclude}
    targets, matches, seen = [], [], set()
    for title in titles:
        if title in seen or title.lower() in skip:
            continue
        low = title.lower()
        if target_key and target_key in low:
            seen.add(title)
            targets.append((title, "target"))
        elif any(term in low for term in terms):
            seen.add(title)
            matches.append((title, "match"))
    return targets + matches


def list_windows(name_filter, *, window_title: str = DEFAULT_WINDOW_TITLE,
                 timeout: float = 30.0, exclude=(), run=None) -> list:
    """Return ``[(title, status)]`` for visible terminal windows (see
    ``select_windows``). ``run`` runs the PowerShell lister and returns its
    stdout — injectable so the GUI panel is testable without a real shell."""
    run = run or _run_list
    return select_windows(_parse_titles(run(timeout)), name_filter, window_title, exclude=exclude)


def _run_list(timeout: float) -> str:
    try:
        proc = subprocess.run(
            [_powershell_bin(), "-NoProfile", "-NonInteractive", "-Command", build_list_script()],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("failed to list windows: %s" % e) from e
    if proc.returncode != 0:
        raise RuntimeError("window list failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip()))
    return proc.stdout
