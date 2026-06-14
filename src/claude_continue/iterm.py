"""Broadcast a line of text into matching iTerm2 sessions via AppleScript.

Ported from the original ``claude-continue.sh`` (the ``tell application
"iTerm2"`` loop), with one important addition: a ``skip_busy`` guard built on
iTerm2's per-session ``is processing`` property. A session that is paused on
the usage-limit message reports ``is processing -> false``, so it is eligible;
a session that is actively working reports ``true`` and is skipped — which
stops us injecting ``continue`` into a turn that's mid-flight.
"""

from __future__ import annotations

import subprocess


def _as_str(s: str) -> str:
    """Escape a Python string for embedding in an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _filter_expr(name_filter, session: str | None, all_sessions: bool) -> str:
    """Build the AppleScript boolean that decides if a session matches."""
    if session:
        return '(sessionName contains "%s")' % _as_str(session)
    if all_sessions:
        return "true"
    clauses = ['sessionName contains "%s"' % _as_str(sub) for sub in (name_filter or [])]
    if not clauses:
        return "false"  # misconfigured: match nothing rather than everything
    return "(" + " or ".join(clauses) + ")"


def build_applescript(
    text: str,
    name_filter,
    *,
    skip_busy: bool = True,
    session: str | None = None,
    dry_run: bool = False,
    all_sessions: bool = False,
    force: bool = False,
) -> str:
    """Generate the AppleScript. Returns the newline-joined names it acted on
    (or *would* act on, when ``dry_run``)."""
    match_expr = _filter_expr(name_filter, session, all_sessions)
    effective_skip_busy = skip_busy and not force

    # The action performed on a matched, eligible session.
    write_line = "" if dry_run else 'tell s to write text "%s"\n            ' % _as_str(text)
    action = write_line + "set end of firedNames to sessionName"

    if effective_skip_busy:
        guarded = (
            "if (is processing of s) is false then\n"
            "            " + action + "\n"
            "          end if"
        )
    else:
        guarded = action

    return (
        'tell application "iTerm2"\n'
        "  set firedNames to {}\n"
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        "        try\n"
        "          set sessionName to name of s\n"
        "          if " + match_expr + " then\n"
        "            " + guarded + "\n"
        "          end if\n"
        "        end try\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
        "  set AppleScript's text item delimiters to linefeed\n"
        "  return firedNames as text\n"
        "end tell\n"
    )


def run_applescript(script: str, timeout: float = 30.0) -> list:
    """Run an AppleScript via ``osascript`` and return its output lines."""
    proc = subprocess.run(
        ["osascript", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "osascript failed (%d): %s" % (proc.returncode, (proc.stderr or "").strip())
        )
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def broadcast(
    text: str,
    name_filter,
    *,
    skip_busy: bool = True,
    session: str | None = None,
    dry_run: bool = False,
    all_sessions: bool = False,
    force: bool = False,
    timeout: float = 30.0,
) -> list:
    """Send ``text`` to matching iTerm2 sessions; return the names acted on."""
    script = build_applescript(
        text,
        name_filter,
        skip_busy=skip_busy,
        session=session,
        dry_run=dry_run,
        all_sessions=all_sessions,
        force=force,
    )
    return run_applescript(script, timeout=timeout)
