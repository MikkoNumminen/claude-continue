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

import ntpath
import shutil
import subprocess
from typing import NamedTuple

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
            stdin=subprocess.DEVNULL,  # don't inherit the GUI's std handle (WinError 6 after a fire)
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
#
# Not every claude process is a terminal session, though. The same binary also
# runs as Chrome's native-messaging host (``claude.exe --chrome-native-host``, a
# background helper Chrome spawns with its stdio on named pipes) and as headless
# one-shots / Agent-SDK workers (``claude -p`` / ``--print``) that some OTHER app
# owns. None of those is a paused terminal waiting for "continue" — listing them
# double-counts the panel and makes continue-all write into a console the user
# never sees (observed live: a fire injecting `continue` into the Chrome host,
# read by the user as "my other terminal got flagged"). parse_instances
# classifies each row by its real argv and keeps only terminal sessions.


class Instance(NamedTuple):
    """One logical Claude terminal session: process name (sans ``.exe``), pid,
    and — when readable — the session's working directory ('' when unknown).
    Indexable like the historical ``(name, pid)`` tuples; consumers accept both
    shapes so pre-``cwd`` callers/tests stay valid."""
    name: str
    pid: str
    cwd: str = ""

# "<pid>\t<ppid>\t<ctime>\t<name>\t<cmdline>" per Claude Code process: the native ``claude.exe``,
# or a ``node.exe`` whose command line references the scoped package path
# ``@anthropic-ai/claude-code`` (its node_modules entry, e.g.
# ...\node_modules\@anthropic-ai\claude-code\cli.js). Anchoring to the scoped
# path — not a bare "claude-code" substring — avoids writing `continue` into an
# unrelated node process (e.g. "claude-coder", or a shell that merely mentions
# the name). Residual: `npm install @anthropic-ai/claude-code` itself would also
# match while installing — acceptable since continue-all is opt-in. CommandLine
# reads need no elevation for the user's own processes; reachable from WSL via
# interop. The Python "[\\\\/]" below emits the PowerShell regex "[\\/]" (\ or /).
#
# It also emits ParentProcessId and CreationDate (as a comparable UTC FILETIME) so
# parse_instances can fold a launcher's worker child onto it: the native
# ``claude.exe`` is a shim — ``claude --continue`` resolves the session then
# re-execs ``claude --resume <uuid>`` as a CHILD that SHARES the launcher's console
# (verified via GetConsoleProcessList). Listing both double-counts one session and
# would make continue-all inject "continue" into the one console twice. CreationDate
# guards against PID recycling: Win32_Process reports the *creating* pid and never
# clears it, and Windows reuses pids, so a dead parent's pid can later belong to an
# UNRELATED live Claude — but a real parent is created no later than its child, so
# the fold only fires when parent CreationDate <= child CreationDate (see
# parse_instances). The ``$(if ...)`` leaves the field empty — never throws — for a
# process whose CreationDate is unreadable, and an empty time is never folded.
#
# The server-side ``-Filter`` narrows to the two image names in WQL so each GUI poll
# marshals a handful of processes instead of the whole process table; the node
# CommandLine match stays client-side in Where-Object (WQL has no regex).
#
# The last column is the raw CommandLine, so parse_instances can tell a terminal
# session from a helper (--chrome-native-host) or a headless one-shot (-p /
# --print) by its REAL argv. Embedded CR/LF/TAB are flattened to spaces
# server-side so a prompt containing them can't break the one-line-per-process,
# tab-separated protocol (or forge a row); the flattening swaps one argv
# whitespace separator for another, so it can't move the exact-token flags the
# classifier looks for in or out of an argument.
_INSTANCES_SCRIPT = (
    "Get-CimInstance Win32_Process -Filter \"Name='claude.exe' OR Name='node.exe'\" | "
    "Where-Object { $_.Name -eq 'claude.exe' -or "
    "($_.Name -eq 'node.exe' -and $_.CommandLine -match '@anthropic-ai[\\\\/]claude-code') } | "
    "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t"
    "$(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() })`t$($_.Name)`t"
    "$($_.CommandLine -replace '[\\r\\n\\t]', ' ')\" }"
)


# Exact argv tokens that mark a claude process as something other than an
# interactive terminal session. `--chrome-native-host` is the Chrome extension's
# native-messaging helper (no terminal at all); `-p` / `--print` is headless
# one-shot / Agent-SDK mode — a claude some OTHER program owns and reads, where
# an injected "continue" would land in that program's console, not a paused
# session. A prior substring-regex attempt at this was rejected (prompt text can
# contain "-p"); matching whole argv tokens from a real command-line parse is
# what makes it safe — a prompt is ONE token, quotes and all, and can never
# equal a bare flag.
_NON_SESSION_ARGS = ("--chrome-native-host", "-p", "--print")


def _argv_from_cmdline(cmdline: str) -> list:
    """Split a Windows command line into argv, following CommandLineToArgvW's
    rules: the program-name token ends at the closing quote or first whitespace
    (no escape processing); after it, 2n backslashes before a quote collapse to
    n, 2n+1 escape the quote, and ``""`` inside quotes emits a literal quote.
    Pure, so classification is testable on any platform. Divergence on
    pathological input is acceptable — the caller treats any surprise as "keep
    the session"."""
    s = (cmdline or "").strip()
    if not s:
        return []
    if s[0] == '"':
        end = s.find('"', 1)
        end = len(s) if end == -1 else end
        argv, i = [s[1:end]], end + 1
    else:
        cuts = [c for c in (s.find(" "), s.find("\t")) if c != -1]
        end = min(cuts) if cuts else len(s)
        argv, i = [s[:end]], end
    cur: list = []
    in_quotes = started = False
    while i < len(s):
        c = s[i]
        if c == "\\":
            n = 0
            while i < len(s) and s[i] == "\\":
                n, i = n + 1, i + 1
            if i < len(s) and s[i] == '"':
                cur.append("\\" * (n // 2))
                if n % 2:  # odd: the quote is escaped -> literal
                    cur.append('"')
                    i += 1
                # even: leave the quote for the branch below (it toggles)
            else:
                cur.append("\\" * n)
            started = True
        elif c == '"':
            if in_quotes and i + 1 < len(s) and s[i + 1] == '"':
                cur.append('"')  # "" inside quotes = one literal quote
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
            started = True
        elif c in " \t" and not in_quotes:
            if started:
                argv.append("".join(cur))
                cur, started = [], False
            i += 1
        else:
            cur.append(c)
            started = True
            i += 1
    if started:
        argv.append("".join(cur))
    return argv


def _is_terminal_session(cmdline: str) -> bool:
    """False only when the command line PROVES this claude process is not an
    interactive terminal session (see ``_NON_SESSION_ARGS``). Biased toward
    keeping: no command line (legacy rows, unreadable) or any parse surprise
    means True — a wrongly-dropped row is a session silently never resumed (the
    cardinal sin here), while a wrongly-kept one costs at most a stray keystroke
    into a console. argv[0] (the program path) is never a flag, so it's skipped."""
    if not (cmdline or "").strip():
        return True
    argv = _argv_from_cmdline(cmdline)
    return not any(tok in _NON_SESSION_ARGS for tok in argv[1:])


def build_instances_script() -> str:
    return _INSTANCES_SCRIPT


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    return name[:-4] if name.lower().endswith(".exe") else name


def parse_instances(stdout: str) -> list:
    """Parse the lister output into ``[Instance]`` — one entry per *logical*
    Claude terminal session, name without the ``.exe`` suffix (e.g. "claude").
    Order-stable. ``cwd`` is always '' here — the command-line protocol can't
    carry it; ``list_claude_instances`` fills it in from the live process.

    Each line is ``"<pid>\\t<ppid>\\t<ctime>\\t<name>\\t<cmdline>"`` where ``ctime``
    is the creation time as a comparable UTC FILETIME (possibly empty) and
    ``cmdline`` is the process's command line (CR/LF/TAB flattened server-side).
    Shorter legacy rows are still accepted — without a cmdline the row is never
    classified away, and without a ctime never folded (see below) — so this stays
    drop-in for callers/tests that predate the columns.

    **Kind classification.** A row whose argv shows it is not a terminal session
    (``_is_terminal_session``: Chrome's ``--chrome-native-host`` helper, headless
    ``-p``/``--print`` one-shots and SDK workers) is dropped — those consoles
    belong to Chrome or to whatever app spawned the worker, so "continue" written
    there lands in a console no user is looking at, and the panel row shows a
    "terminal" that doesn't exist. Matching is whole-argv-token only, biased
    toward keeping (see the docstrings above).

    **Launcher/worker fold.** The native ``claude.exe`` is a shim — a ``claude
    --continue`` resolves the session then re-execs ``claude --resume <uuid>`` as a
    CHILD that shares the launcher's console (proven via GetConsoleProcessList).
    Listing both shows one session as two panel rows AND makes continue-all write
    "continue" into that single console twice. So a process whose PARENT is also a
    matched Claude process is folded onto the parent and dropped — leaving one row
    for the pair, which keeps the panel and the action in agreement and the
    keystroke single. Deduped by pid first; the fold is a second pass so listing
    order (child before parent) doesn't matter.

    The fold is gated on creation time to stay correct under PID recycling: a dead
    parent's pid can later belong to an UNRELATED live Claude, making a separate
    session look like a worker. A real parent is created no later than its child, so
    we fold only when the matched parent's ctime <= the child's; a recycled "parent"
    is newer and the child is kept. When either time is unknown the row is kept —
    never drop a live session on a guess.

    Residuals (both safe-direction — at worst a transient duplicate keystroke or a
    one-cycle miss that the next poll heals, never a permanently dropped session):
    a Claude that deliberately spawns another Claude in its OWN new console would
    still be folded (same pid topology, parent older), but Claude Code sessions are
    launched by shells (an unmatched parent), not by other Claudes, so in practice
    nothing real is lost; and the fold keeps the launcher, betting it outlives its
    worker — if it instead exits between this listing and the fire, that session is
    skipped for one cycle and resumes on the next poll."""
    rows, seen = [], set()  # rows: (pid, ppid, ctime, name) in listing order
    for ln in (stdout or "").splitlines():
        if "\t" not in ln:
            continue
        parts = ln.split("\t")
        if len(parts) >= 5:
            pid, ppid, ctime, name = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3]
            # 5th column: the command line. The lister flattened embedded tabs, so
            # normally this is one part; re-joining any tail keeps a hand-built row
            # with a raw tab faithful. Not a terminal session -> not listed.
            if not _is_terminal_session("\t".join(parts[4:])):
                continue
        elif len(parts) == 4:
            pid, ppid, ctime, name = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3]
        elif len(parts) == 3:
            pid, ppid, ctime, name = parts[0].strip(), parts[1].strip(), "", parts[2]
        else:
            pid, ppid, ctime, name = parts[0].strip(), "", "", parts[1]
        if not pid.isdigit() or pid in seen:
            continue
        seen.add(pid)
        rows.append((pid, ppid, ctime, name))
    def _ft(s):  # a FILETIME field -> int, or None when empty/non-numeric (kept)
        try:
            return int(s)
        except (TypeError, ValueError):  # incl. isdigit-True Unicode digits int() rejects
            return None

    ct_by_pid = {pid: _ft(ctime) for pid, _ppid, ctime, _name in rows}
    out = []
    for pid, ppid, ctime, name in rows:
        parent_ct, child_ct = ct_by_pid.get(ppid), _ft(ctime)
        # Fold a worker child onto its launcher only when the matched parent is the
        # real one — created no later than the child. A recycled/stale ppid points
        # at a newer process and fails this, so the child (a live session) is kept;
        # an unknown time on either side also keeps the row (never fold on a guess).
        if parent_ct is not None and child_ct is not None and parent_ct <= child_ct:
            continue
        out.append(Instance(_clean_name(name), pid))
    return out


def list_claude_instances(*, timeout: float = 30.0, run=None, cwd_fn=None) -> list:
    """Return ``[Instance]`` for running Claude Code terminal sessions (native
    ``claude.exe`` or the npm node CLI), excluding the claude-continue app and
    non-terminal claude processes (see ``parse_instances``). Each instance's
    ``cwd`` is filled in best-effort so the panel can say WHICH terminal each row
    is ("claude · HRManager") and skip-dirs can match — '' when unreadable, which
    only ever degrades to the old anonymous row. ``run`` runs the PowerShell
    lister and returns its stdout; ``cwd_fn`` maps a pid to its working directory
    — both injectable so the panel is testable without a real shell/process."""
    run = run or _run_instances
    instances = parse_instances(run(timeout))
    if cwd_fn is None:
        cwd_fn = _cwd_of_pid if osenv.is_windows() else None
    if cwd_fn is None:
        return instances
    out = []
    for inst in instances:
        try:
            cwd = cwd_fn(inst.pid) or ""
        except Exception:  # noqa: BLE001 - cwd is best-effort garnish, never fatal
            cwd = ""
        out.append(inst._replace(cwd=cwd))
    return out


def _run_instances(timeout: float) -> str:
    try:
        proc = subprocess.run(
            [_powershell_bin(), "-NoProfile", "-NonInteractive", "-Command", build_instances_script()],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,  # don't inherit the GUI's std handle (WinError 6 after a fire)
            **osenv.no_window_kwargs(),  # no console-window flash from the GUI poll
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("failed to list Claude instances: %s" % e) from e
    if proc.returncode != 0:
        raise RuntimeError("instance list failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip()))
    return proc.stdout


def _cwd_of_pid(pid) -> str:
    """Best-effort current directory of another process, '' on any failure.

    Windows has no supported API for another process's cwd; the standard trick
    (what Process Explorer does) is reading it out of the target's PEB:
    NtQueryInformationProcess gives the PEB address, and
    PEB->ProcessParameters->CurrentDirectory is a UNICODE_STRING in the target's
    memory. Offsets below are the stable, documented x64 layout (ProcessParameters
    at PEB+0x20, CurrentDirectory at +0x38), so a non-64-bit build skips rather
    than misread. Needs only PROCESS_QUERY_INFORMATION|PROCESS_VM_READ, which the
    user has on their own processes. Purely a read — unlike AttachConsole this
    never touches our console or the target. ctypes is imported lazily so the
    module still imports on other platforms (where this is never called)."""
    import ctypes
    from ctypes import wintypes

    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return ""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]

    class _ProcBasicInfo(ctypes.Structure):
        _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                    ("Reserved3", ctypes.c_void_p)]

    k32.OpenProcess.restype = wintypes.HANDLE  # pointer-sized, like CreateFileW above
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    handle = k32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        return ""
    try:
        def read(addr, size):
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            ok = k32.ReadProcessMemory(wintypes.HANDLE(handle), ctypes.c_void_p(addr),
                                       buf, size, ctypes.byref(got))
            return buf.raw if ok and got.value == size else None

        pbi = _ProcBasicInfo()
        status = ntdll.NtQueryInformationProcess(wintypes.HANDLE(handle), 0,  # ProcessBasicInformation
                                                 ctypes.byref(pbi), ctypes.sizeof(pbi), None)
        if status != 0 or not pbi.PebBaseAddress:
            return ""
        raw = read(int(pbi.PebBaseAddress) + 0x20, 8)  # PEB->ProcessParameters
        if raw is None:
            return ""
        params = int.from_bytes(raw, "little")
        raw = read(params + 0x38, 16)  # CurrentDirectory: UNICODE_STRING {Len, Max, pad, Buffer}
        if raw is None:
            return ""
        length = int.from_bytes(raw[0:2], "little")
        buffer = int.from_bytes(raw[8:16], "little")
        if not buffer or not 0 < length <= 0x8000:  # MAX_PATH-ish sanity bound
            return ""
        raw = read(buffer, length)
        if raw is None:
            return ""
        return raw.decode("utf-16-le", "replace")
    finally:
        k32.CloseHandle(wintypes.HANDLE(handle))


def dir_label(cwd: str) -> str:
    """Short display name for a session's working directory — the final path
    component (e.g. "HRManager"), '' when unknown. ntpath (not os.path) so
    Windows paths are handled identically on the Linux CI. Pure."""
    if not (cwd or "").strip():
        return ""
    norm = ntpath.normpath(cwd.strip())
    return ntpath.basename(norm) or norm  # a drive root ("C:\\") has no basename


def dir_skipped(cwd: str, skip_dirs) -> bool:
    """True when a session working in ``cwd`` is excluded by the user's
    ``skip_dirs``. An entry containing a path separator (or drive colon) matches
    that directory itself or anything under it; a bare entry matches the folder
    NAME, so "HRManager" excludes ``D:\\...\\HRManager`` without typing the full
    path. Case-insensitive throughout (Windows paths). An unknown cwd ('') never
    matches — a session we can't identify keeps being resumed rather than
    silently dropped. Pure (ntpath), so testable on any platform."""
    if not (cwd or "").strip() or not skip_dirs:
        return False
    norm = ntpath.normcase(ntpath.normpath(cwd.strip()))
    base = ntpath.basename(norm)
    for entry in skip_dirs:
        e = str(entry or "").strip()
        if not e:
            continue
        if "\\" in e or "/" in e or ":" in e:
            en = ntpath.normcase(ntpath.normpath(e))
            if norm == en or norm.startswith(en.rstrip("\\") + "\\"):
                return True
        elif ntpath.normcase(e) == base:
            return True
    return False


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
            stdin=subprocess.DEVNULL,  # don't inherit the GUI's std handle (WinError 6 after a fire)
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
# STD_INPUT/OUTPUT/ERROR_HANDLE — the (DWORD)-10/-11/-12 selectors for Get/SetStdHandle.
_STD_HANDLES = (0xFFFFFFF6, 0xFFFFFFF5, 0xFFFFFFF4)
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
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    # AttachConsole/FreeConsole REWRITE this process's std handles. A windowed GUI
    # has no console and no parent console to re-attach to, so the FreeConsole +
    # AttachConsole(-1) restore in the finally below FAILS, leaving STDIN/STDOUT/STDERR
    # pointing at the now-freed Claude console. After that the GUI's next
    # subprocess.run inherits those stale handles and CreateProcess fails with
    # "[WinError 6] The handle is invalid" — silently breaking the ccusage and
    # instance-list polls for the rest of the session once a fire has run. Snapshot the
    # handles now and put them back if the re-attach can't (a console-less GUI),
    # returning the process to its pre-fire state.
    saved_std = [k32.GetStdHandle(h) for h in _STD_HANDLES]
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
        # Re-attach to the parent's console (a CLI caller) to restore its stdout. When
        # that fails — a windowed GUI launched from Explorer has no parent console —
        # the std handles are left dangling at the freed Claude console, so restore the
        # snapshot taken before the dance. Without this, every later subprocess.run in
        # the GUI dies with WinError 6 (see the snapshot comment above).
        if not k32.AttachConsole(_ATTACH_PARENT_PROCESS):
            for h, val in zip(_STD_HANDLES, saved_std):
                k32.SetStdHandle(h, val)


def continue_instances(text: str, *, instances=None, dry_run: bool = False,
                       timeout: float = 30.0, inject=None, list_fn=None, is_alive=None,
                       skip_dirs=()) -> list:
    """Send ``text``+Enter to EVERY running Claude session by injecting into each
    one's console input. Returns one label per session acted on.

    A session whose working directory matches ``skip_dirs`` (see ``dir_skipped``)
    is left untouched — the user's way of saying "that terminal is doing its own
    thing, don't resume it". The GUI panel renders the same match as "skipped",
    keeping the panel and the action in agreement.

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
    ``instances``/``inject``/``list_fn``/``is_alive`` are injectable for tests;
    ``instances`` may be ``Instance``s or legacy ``(name, pid)`` tuples."""
    list_fn = list_fn or list_claude_instances
    inject = inject or _inject_one
    is_alive = is_alive or osenv.pid_alive
    if instances is None:
        instances = list_fn(timeout=timeout)
    keys = text + "\r"
    out, failures = [], []
    for inst in instances:
        name, pid = inst[0], inst[1]
        cwd = inst[2] if len(inst) > 2 else ""
        if dir_skipped(cwd, skip_dirs):
            continue  # user-excluded project — leave that terminal alone
        where = dir_label(cwd)
        label = ("continue -> %s (pid %s, %s)" % (name, pid, where) if where
                 else "continue -> %s (pid %s)" % (name, pid))
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
    if failures:
        if not out:
            # nothing landed at all — surface it so the caller can retry/degrade
            raise RuntimeError("; ".join(failures))
        # PARTIAL failure: some sessions resumed, some didn't. Don't silently drop the
        # rest (a failed session is left paused with no signal). Log at WARNING via the
        # project logger (the watch daemon's stdout sink; Python's last-resort stderr
        # otherwise) without aborting the successful resumes.
        from .log import get_logger
        get_logger().warning(
            "continue-all: %d of %d session(s) failed to resume: %s",
            len(failures), len(out) + len(failures), "; ".join(failures),
        )
    return out
