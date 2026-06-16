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

from . import osenv

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
            **osenv.no_window_kwargs(),
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


# --- Claude instance listing (the GUI's "Claude instances" panel on Windows) ---
#
# The macOS panel lists iTerm2 *sessions* running Claude; the honest Windows
# analogue lists the running Claude Code *processes*. Claude Code runs either as
# the native ``claude.exe`` or as the npm node CLI (its command line names the
# ``claude-code`` package); the claude-continue app itself is never one of these,
# so it can't list itself. There's no Windows equivalent of iTerm2's per-session
# "is processing" flag, so instances are listed without a working/idle marker.

# "<pid>\t<name>" per Claude Code process: the native ``claude.exe``, or a
# ``node.exe`` whose command line names the claude-code package (npm install).
# The command-line match is SCOPED to node.exe on purpose — otherwise the very
# PowerShell process running this query self-matches (its own command line
# contains the literal "claude-code"), and so would any shell that merely
# mentions the package path. CommandLine reads need no elevation for the user's
# own processes. Built into Windows; reachable from WSL via interop.
_INSTANCES_SCRIPT = (
    "Get-CimInstance Win32_Process | "
    "Where-Object { $_.Name -eq 'claude.exe' -or "
    "($_.Name -eq 'node.exe' -and $_.CommandLine -match 'claude-code') } | "
    "ForEach-Object { \"$($_.ProcessId)`t$($_.Name)\" }"
)


def build_instances_script() -> str:
    return _INSTANCES_SCRIPT


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    return name[:-4] if name.lower().endswith(".exe") else name


def parse_instances(stdout: str) -> list:
    """Parse the ``"<pid>\\t<name>"`` lister output into ``[(name, pid)]`` — name
    without the ``.exe`` suffix (e.g. "claude"). Deduped by pid, order-stable."""
    out, seen = [], set()
    for ln in (stdout or "").splitlines():
        if "\t" not in ln:
            continue
        pid, name = ln.split("\t", 1)
        pid = pid.strip()
        if not pid.isdigit() or pid in seen:
            continue
        seen.add(pid)
        out.append((_clean_name(name), pid))
    return out


def list_claude_instances(*, timeout: float = 30.0, run=None) -> list:
    """Return ``[(name, pid)]`` for running Claude Code processes (native
    ``claude.exe`` or the npm node CLI), excluding the claude-continue app.
    ``run`` runs the PowerShell lister and returns its stdout — injectable so the
    panel is testable without a real shell."""
    run = run or _run_instances
    return parse_instances(run(timeout))


def _run_instances(timeout: float) -> str:
    try:
        proc = subprocess.run(
            [_powershell_bin(), "-NoProfile", "-NonInteractive", "-Command", build_instances_script()],
            capture_output=True,
            text=True,
            timeout=timeout,
            **osenv.no_window_kwargs(),  # no console-window flash from the GUI poll
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("failed to list Claude instances: %s" % e) from e
    if proc.returncode != 0:
        raise RuntimeError("instance list failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip()))
    return proc.stdout


# --- Window-title listing (used by the doctor to vet the keystroke target) ---
#
# send_keystroke activates a window via WScript.Shell.AppActivate(title), which
# only finds a window whose title EQUALS the string or BEGINS WITH it. If nothing
# matches, the keystroke goes nowhere (or into the wrong window) — the #1 reason a
# keystroke watch silently does nothing, because Windows Terminal's window title
# is the active *tab's* title, not the literal "Windows Terminal". The doctor
# enumerates open window titles so it can tell the user honestly whether their
# --window-title will hit anything. We read Get-Process MainWindowTitle rather
# than calling AppActivate to probe — listing doesn't steal focus.

_WINDOW_TITLES_SCRIPT = (
    "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | "
    "ForEach-Object { $_.MainWindowTitle }"
)


def build_window_titles_script() -> str:
    return _WINDOW_TITLES_SCRIPT


def parse_window_titles(stdout: str) -> list:
    """Parse the lister output into a list of non-empty window titles —
    order-stable and de-duplicated."""
    out, seen = [], set()
    for ln in (stdout or "").splitlines():
        title = ln.strip()
        if title and title not in seen:
            seen.add(title)
            out.append(title)
    return out


def window_match(target: str, titles) -> bool:
    """True if ``AppActivate(target)`` would plausibly find one of ``titles``.
    AppActivate matches a title that equals ``target`` or begins with it (and is
    case-insensitive in practice); mirror that so the doctor's keystroke check is
    a faithful probe, not a guess. ``startswith`` covers the equality case too."""
    t = (target or "").strip().lower()
    if not t:
        return False
    return any((title or "").strip().lower().startswith(t) for title in titles)


def list_window_titles(*, timeout: float = 30.0, run=None) -> list:
    """Return the titles of all top-level windows that have a visible title.
    ``run`` runs the PowerShell lister and returns its stdout — injectable so the
    doctor check is testable without a real shell."""
    run = run or _run_window_titles
    return parse_window_titles(run(timeout))


def _run_window_titles(timeout: float) -> str:
    try:
        proc = subprocess.run(
            [_powershell_bin(), "-NoProfile", "-NonInteractive", "-Command", build_window_titles_script()],
            capture_output=True,
            text=True,
            timeout=timeout,
            **osenv.no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("failed to list window titles: %s" % e) from e
    if proc.returncode != 0:
        raise RuntimeError("window-title list failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip()))
    return proc.stdout


# --- Continue EVERY Claude session via console-input injection (the reliable path) ---
#
# Resuming more than one Claude session in a single terminal is the hard part on
# Windows: sessions multiplexed as tabs OR split panes in one Windows Terminal
# window can't be reached by SendKeys (it only hits the focused tab/pane, and
# there's no API to type into a background one). The reliable mechanism is to
# bypass the window entirely and write to each process's CONSOLE INPUT directly:
# AttachConsole(pid) attaches us to that Claude's (pseudo)console, then
# WriteConsoleInput injects "continue<Enter>" straight into its input buffer.
# This targets each session by PID — no focus, no tab/pane cycling, no window
# title — and works for split panes, tabs, separate windows, and even an
# unfocused/background terminal. (Verified against Windows Terminal's ConPTY in
# both cooked and raw input modes.)

_ATTACH_PARENT_PROCESS = 0xFFFFFFFF  # AttachConsole(-1): reattach to our own console
_KEY_EVENT = 0x0001
_VK_RETURN = 0x0D


def _utf16_units(text: str) -> list:
    """Split ``text`` into UTF-16 code units (each a 1-character string). A console
    INPUT_RECORD's ``UnicodeChar`` is a single UTF-16 code unit, so a non-BMP char
    (e.g. an emoji in a customized resume text) must be sent as its two surrogate
    halves — assigning the whole char to a WCHAR would raise TypeError. Pure, so
    it's testable without ctypes / Windows."""
    raw = text.encode("utf-16-le")
    return [raw[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(raw), 2)]


def _inject_one(pid, text: str) -> None:
    """Write ``text`` to process ``pid``'s console input via AttachConsole +
    WriteConsoleInput. Raises RuntimeError if the process can't be attached (it
    exited, or denies access). Restores our own console afterward so a CLI caller
    keeps its stdout. Windows-only — ctypes is imported lazily so this module
    still imports on other platforms (where it's never called)."""
    import ctypes
    from ctypes import wintypes

    class _UChar(ctypes.Union):
        _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]

    class _KeyEvent(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                    ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                    ("uChar", _UChar), ("dwControlKeyState", wintypes.DWORD)]

    class _InputRecord(ctypes.Structure):
        class _Ev(ctypes.Union):
            _fields_ = [("KeyEvent", _KeyEvent)]
        _anonymous_ = ("Event",)
        _fields_ = [("EventType", wintypes.WORD), ("Event", _Ev)]

    records = []
    for cu in _utf16_units(text):  # UTF-16 code units: non-BMP chars stay valid WCHARs
        for down in (1, 0):  # each unit needs a key-down then key-up record
            r = _InputRecord()
            r.EventType = _KEY_EVENT
            r.KeyEvent.bKeyDown = down
            r.KeyEvent.wRepeatCount = 1
            r.KeyEvent.wVirtualKeyCode = _VK_RETURN if cu == "\r" else 0
            r.KeyEvent.uChar.UnicodeChar = cu
            records.append(r)

    # WinDLL / get_last_error are Windows-only in typeshed, so mypy (which CI runs
    # on Linux) needs the ignore — same pattern as osenv.pid_alive.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    last_err = ctypes.get_last_error  # type: ignore[attr-defined]
    # CreateFileW returns a HANDLE (pointer-sized): pin the restype so it isn't
    # truncated to 32 bits on Win64 (the ctypes default c_int would corrupt a
    # high handle value).
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.FreeConsole()  # a process can only be attached to one console at a time
    try:
        if not k32.AttachConsole(int(pid)):
            raise RuntimeError("could not attach to pid %s (exited?), err=%s" % (pid, last_err()))
        handle = k32.CreateFileW("CONIN$", 0xC0000000, 0x3, None, 0x3, 0, None)
        if not handle or handle == (2 ** 64 - 1):  # NULL or INVALID_HANDLE_VALUE
            raise RuntimeError("CONIN$ open failed for pid %s, err=%s" % (pid, last_err()))
        arr = (_InputRecord * len(records))(*records)
        written = wintypes.DWORD(0)
        ok = k32.WriteConsoleInputW(wintypes.HANDLE(handle), arr, len(records), ctypes.byref(written))
        k32.CloseHandle(wintypes.HANDLE(handle))
        if not ok:
            raise RuntimeError("WriteConsoleInput failed for pid %s, err=%s" % (pid, last_err()))
    finally:
        k32.FreeConsole()
        k32.AttachConsole(_ATTACH_PARENT_PROCESS)  # best-effort: restore CLI stdout


def continue_instances(text: str, *, instances=None, dry_run: bool = False,
                       timeout: float = 30.0, inject=None, list_fn=None, is_alive=None) -> list:
    """Send ``text``+Enter to EVERY running Claude session by injecting into each
    one's console input. Returns one label per session acted on.

    Best-effort per session: a process that exited (or denies attach) is skipped,
    so one dead session never aborts the rest. We re-check ``pid_alive`` right
    before attaching to shrink the TOCTOU window where a just-exited PID could be
    recycled and the keystroke land in an unrelated console (it can't be fully
    closed — only AttachConsole's own "must own a console" check bounds the rest).

    NOTE: unlike the iTerm2/tmux paths, this has NO skip-busy guard — Windows
    exposes no per-session "is processing" flag. So if the watch loop's verify
    retry re-fires (ccusage's reset estimate was early), a session that already
    resumed and is mid-work can receive a second `continue`. That's the platform
    tradeoff for resuming sessions that SendKeys can't reach at all.
    ``instances``/``inject``/``list_fn``/``is_alive`` are injectable for tests."""
    list_fn = list_fn or list_claude_instances
    inject = inject or _inject_one
    is_alive = is_alive or osenv.pid_alive
    if instances is None:
        instances = list_fn(timeout=timeout)
    keys = text + "\r"
    out, failures = [], []
    for name, pid in instances:
        label = "continue -> %s (pid %s)" % (name, pid)
        if dry_run:
            out.append(label)
            continue
        try:
            if not is_alive(int(pid)):
                continue  # exited between listing and now — nothing to resume, skip quietly
        except (ValueError, OSError):
            pass  # liveness check is only a narrowing optimization; fall through to inject
        try:
            inject(pid, keys)
            out.append(label)
        except (RuntimeError, OSError) as e:  # noqa: PERF203 - per-session isolation is the point
            failures.append("%s: %s" % (label, e))
    if not out and failures:
        # nothing landed at all — surface it so the caller can retry/degrade
        raise RuntimeError("; ".join(failures))
    return out
