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
    """Return [{id, session, window, title, cmd}] for matching Claude tmux panes."""
    out = _tmux(["list-panes", "-a", "-F", _FMT], timeout=timeout)
    return _parse_panes(out, name_filter, session, all_sessions)


def _is_busy(pane_id: str, busy_pattern: str, timeout: float) -> bool:
    """True if the pane's visible content shows Claude is mid-turn."""
    if not busy_pattern:
        return False
    content = _tmux(["capture-pane", "-p", "-t", pane_id], timeout=timeout)
    # Claude's "esc to interrupt" footer renders at the bottom of the pane. Only
    # inspect the last few non-blank lines so the marker appearing earlier in the
    # transcript (a paste, a doc, our own past output) can't false-trip skip-busy.
    lines = [ln for ln in content.splitlines() if ln.strip()]
    tail = "\n".join(lines[-3:]).lower()
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
        if effective_skip_busy and _is_busy(pane["id"], busy_pattern, timeout):
            continue
        if not dry_run:
            # -l sends the text literally (no key-name interpretation); `--` ends
            # option parsing so a text starting with '-' can't be read as a flag.
            # Enter is a separate key event that submits it.
            _tmux(["send-keys", "-t", pane["id"], "-l", "--", text], timeout=timeout)
            _tmux(["send-keys", "-t", pane["id"], "Enter"], timeout=timeout)
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
        status = "working" if _is_busy(pane["id"], busy_pattern, timeout) else "idle"
        out.append((_label(pane), status))
    return out
