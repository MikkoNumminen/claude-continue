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
# one will the keystroke land in? ``AppActivate`` activates a window whose title
# equals, begins with, or ends with the target (NOT any substring), so we mirror
# exactly that here — the panel then shows the user whether their ``--window-title``
# target is actually present, and never marks one the keystroke couldn't hit.

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


def _appactivate_matches(title_low: str, key_low: str) -> bool:
    """Whether ``WScript.Shell.AppActivate(key)`` would activate a window titled
    ``title``. Its real contract is exact, else begins-with, else ends-with (all
    case-insensitive) — NOT a general substring match. ``select_windows`` uses
    exactly this for the "target" class so the panel never marks a window the
    keystroke action couldn't actually land in. Both args are pre-lowercased."""
    return title_low == key_low or title_low.startswith(key_low) or title_low.endswith(key_low)


def select_windows(titles, name_filter, window_title: str, *, exclude=()) -> list:
    """Classify visible window titles for the keystroke panel. Pure/testable.

    Returns ``[(title, status)]`` where status is:
      - "target": ``AppActivate(window_title)`` would land here — an exact /
        begins-with / ends-with title match (see ``_appactivate_matches``);
      - "match":  the title contains one of the ``name_filter`` terms (a likely
        Claude terminal) but is not the keystroke target.

    Titles in ``exclude`` are dropped (case-insensitive, exact) — the GUI passes
    its own window title so the app doesn't list itself as a candidate terminal.
    Targets come first; titles matching neither are dropped. All matching is
    case-insensitive, de-duplicated by title (case-insensitively), order-stable.
    """
    target_key = (window_title or "").lower()
    terms = [t.lower() for t in (name_filter or []) if t]
    skip = {x.lower() for x in exclude}
    targets, matches, seen = [], [], set()
    for title in titles:
        low = title.lower()
        if low in seen or low in skip:
            continue
        if target_key and _appactivate_matches(low, target_key):
            seen.add(low)
            targets.append((title, "target"))
        elif any(term in low for term in terms):
            seen.add(low)
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
