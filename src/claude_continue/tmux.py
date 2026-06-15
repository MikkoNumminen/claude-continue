"""Resume Claude sessions running inside tmux — the terminal-agnostic path.

Unlike the iTerm2 broadcast, tmux works in *any* terminal (Terminal.app,
Ghostty, Warp, kitty, GNOME Terminal, Konsole, …) on macOS and Linux, as long as
Claude Code runs inside a tmux pane. We target panes precisely by id, so there's
no focus-stealing and no Accessibility permission.

tmux has no `is processing` flag like iTerm2, so "busy" is detected by capturing
the pane's visible content and matching a pattern Claude shows while it's working
(default "esc to interrupt") — the inverse of iTerm2's momentary is_processing
sample, and just as much a heuristic.
"""

from __future__ import annotations

import shutil
import subprocess

DEFAULT_BUSY_PATTERN = "esc to interrupt"

# pane_id is a stable handle (e.g. "%3"); the names are what we filter/display on.
_FMT = "#{pane_id}\t#{session_name}\t#{window_name}\t#{pane_title}"

# How many non-blank lines from the bottom of the visible pane to scan for the
# busy marker. Claude's "esc to interrupt" footer sits ABOVE the input box
# (spinner + box border + prompt + hint), so it's several lines up from the very
# bottom; scan a generous tail so a working pane is reliably seen as busy. The
# bias is deliberate: a false "busy" only defers a resume to the next retry,
# whereas a false "idle" would type into a live turn.
_FOOTER_LINES = 12


class TmuxError(RuntimeError):
    """tmux was missing, timed out, or returned an error."""


def _tmux(args, *, timeout: float) -> str:
    exe = shutil.which("tmux")
    if not exe:
        raise TmuxError("tmux not found on PATH")
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:  # raced away between which() and run()
        raise TmuxError("tmux not found: %s" % e) from e
    except subprocess.TimeoutExpired as e:
        raise TmuxError("tmux timed out after %ss" % timeout) from e
    except OSError as e:
        raise TmuxError("failed to run tmux: %s" % e) from e
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # No server / no sessions just means "nothing to resume", not a failure.
        low = err.lower()
        if "no server running" in low or "no current session" in low or "no sessions" in low:
            return ""
        raise TmuxError("tmux failed (%d): %s" % (proc.returncode, err))
    return proc.stdout


def _matches(name_filter, session, all_sessions, sess, win, title) -> bool:
    """Decide whether a pane should be targeted."""
    if session:
        # --session targets the tmux SESSION grouping by name (matches iterm.py
        # and the CLI help), NOT incidental text in a window name or pane title.
        return session.lower() in sess.lower()
    if all_sessions:
        return True
    subs = name_filter or []
    if not subs:
        return False  # misconfigured: match nothing rather than everything
    # Match the fields Claude actually labels — its title-bar marker lands in
    # pane_title (and sometimes window_name). Deliberately exclude session_name:
    # it's usually the working-dir (e.g. "claude-continue"), which would
    # otherwise match every pane in the session, including plain shells.
    hay = (win + " " + title).lower()
    return any(sub.lower() in hay for sub in subs)


def _parse_panes(out, name_filter, session, all_sessions) -> list:
    panes = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 4:
            continue
        pane_id, sess, win, title = parts[0], parts[1], parts[2], parts[3]
        if _matches(name_filter, session, all_sessions, sess, win, title):
            panes.append({"id": pane_id, "session": sess, "window": win, "title": title})
    return panes


def list_panes(name_filter, *, session=None, all_sessions=False, timeout: float = 10.0) -> list:
    """Return [{id, session, window, title}] for matching Claude tmux panes."""
    out = _tmux(["list-panes", "-a", "-F", _FMT], timeout=timeout)
    return _parse_panes(out, name_filter, session, all_sessions)


def _is_busy(pane_id: str, busy_pattern: str, timeout: float) -> bool:
    """True if the pane's visible content shows Claude is mid-turn.

    Each call is its own `capture-pane` (one per pane) — inherent to tmux, but
    panes are few and capture-pane is cheap.
    """
    if not busy_pattern:
        return False
    content = _tmux(["capture-pane", "-p", "-t", pane_id], timeout=timeout)
    # Scan a generous tail of the visible pane (see _FOOTER_LINES): Claude's
    # "esc to interrupt" marker sits a few lines above the input box, not on the
    # very last line. A paused/limited session shows the limit message there
    # instead, so it still reads as idle and gets resumed.
    lines = [ln for ln in content.splitlines() if ln.strip()]
    tail = "\n".join(lines[-_FOOTER_LINES:]).lower()
    return busy_pattern.lower() in tail


def _label(pane: dict) -> str:
    return pane["title"] or pane["window"] or pane["id"]


def broadcast(
    text: str,
    name_filter,
    *,
    skip_busy: bool = True,
    session: str | None = None,
    dry_run: bool = False,
    all_sessions: bool = False,
    force: bool = False,
    busy_pattern: str = DEFAULT_BUSY_PATTERN,
    timeout: float = 10.0,
) -> list:
    """Send ``text`` + Enter to each matching tmux pane; return the labels acted on."""
    panes = list_panes(name_filter, session=session, all_sessions=all_sessions, timeout=timeout)
    effective_skip_busy = skip_busy and not force
    fired = []
    for pane in panes:
        try:
            if effective_skip_busy and _is_busy(pane["id"], busy_pattern, timeout):
                continue
            if not dry_run:
                # -l sends the text literally (no key-name interpretation); `--` ends
                # option parsing so a text starting with '-' can't be read as a flag.
                # Enter is a separate key event that submits it.
                _tmux(["send-keys", "-t", pane["id"], "-l", "--", text], timeout=timeout)
                _tmux(["send-keys", "-t", pane["id"], "Enter"], timeout=timeout)
        except TmuxError:
            # a pane can vanish between enumeration and use; skip it rather than
            # aborting the whole broadcast (mirrors iTerm2's per-session `try`).
            continue
        fired.append(_label(pane))
    return fired


def list_sessions(
    name_filter,
    *,
    session: str | None = None,
    all_sessions: bool = False,
    busy_pattern: str = DEFAULT_BUSY_PATTERN,
    timeout: float = 10.0,
) -> list:
    """Return [(label, status)] for matching panes; status ∈ {working, idle}.
    Mirrors iterm.list_sessions so the GUI panel can render either source."""
    panes = list_panes(name_filter, session=session, all_sessions=all_sessions, timeout=timeout)
    out = []
    for pane in panes:
        try:
            status = "working" if _is_busy(pane["id"], busy_pattern, timeout) else "idle"
        except TmuxError:
            continue  # pane vanished mid-query; drop it rather than failing the whole panel
        out.append((_label(pane), status))
    return out
