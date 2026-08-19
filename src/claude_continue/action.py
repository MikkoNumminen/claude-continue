"""What to actually *do* at a reset.

Resolution order:
1. ``exec_cmd`` set  -> run it headless (cross-platform; the reliable default on
   Windows/WSL where there's no per-session "type into it" API).
2. ``tmux`` set      -> ``tmux send-keys`` into matching panes (terminal-agnostic:
   any terminal on macOS/Linux, as long as Claude runs inside tmux).
3. ``keystroke`` set -> type ``text`` into a terminal window
   (macOS: iTerm2 broadcast; Windows/WSL: PowerShell SendKeys).
4. otherwise         -> macOS broadcasts to iTerm2 (zero-config resume);
   Windows/WSL/Linux raise ActionError (set --exec, --tmux or --keystroke).

All failures surface as ``ActionError`` so the watch loop can degrade to
re-arm/poll instead of crashing the daemon.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from . import iterm, limits, osenv, tmux, winterm
from .config import Config
from .log import get_logger


class ActionError(Exception):
    """The configured action could not be performed."""


class NothingToResume(Exception):
    """``require_limit`` is on and no session is parked on a spent limit.

    Not an error: it is the gate doing its job. The watch loop treats it as "don't
    fire, re-arm" rather than a failure to report.
    """

    def __init__(self, detail: str = "", retry_at=None):
        super().__init__(detail or "no session is waiting on a spent limit")
        self.detail = detail
        # When the sessions are limited but the reset has not arrived yet, this is
        # the instant they become resumable — far more accurate than ccusage's
        # floor-to-the-hour estimate, so the caller re-arms on it.
        self.retry_at = retry_at


def perform(cfg: Config, dry_run: bool = False) -> list:
    """Execute the configured action. Returns human-readable strings describing
    what was acted on (session names, the keystroke target, or the exec command)."""
    if cfg.exec_cmd:
        return _run_exec(cfg.exec_cmd, dry_run=dry_run)
    # "quota mode": open a fresh usage window headlessly without touching any
    # terminal (the automated version of typing a throwaway message into chat).
    if cfg.start_window:
        return _run_exec(cfg.window_cmd, dry_run=dry_run, label="open window")
    return _resume(cfg, dry_run=dry_run)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Snapshot:
    """What the resume targets look like right now, per Claude Code's transcripts.

    The watch loop verifies a fire against this instead of against ccusage: a
    resumed session stops reporting a limit *immediately*, whereas ccusage cannot
    show a rolled window until its floored five-hour bucket expires (see limits.py).
    """

    known: bool = False  # at least one transcript was readable
    ready: int = 0  # parked on a limit whose reset has passed
    waiting: int = 0  # limited, reset still ahead, and ours to act on
    capped: int = 0  # limited on something `continue` can't clear (a model cap)
    idle: int = 0  # running, not limited
    soonest: Optional[datetime] = None  # earliest future reset among `waiting`
    detail: str = ""

    @property
    def blocked(self) -> int:
        """Sessions still stopped on something this tool can move.

        A capped session is deliberately NOT counted. It is stopped, but no fire will
        ever change that, so counting it would mean a post-fire check could never
        report success while one existed — the resume it just did would read as a
        failure, and the loop would re-arm on a reset that means nothing to it.
        """
        return self.ready + self.waiting


def session_states(cfg: Config, now: Optional[datetime] = None) -> list:
    """``[(label, LimitState)]`` for every session this config would resume.

    Windows continue-all can enumerate live Claude processes and read each one's
    working directory, so it reports exactly the sessions a fire would touch,
    ``skip_dirs`` excluded. Everywhere else there is no per-session working
    directory to be had, so it falls back to recently-active transcripts.
    """
    now = now or _utc_now()
    if cfg.keystroke_all and osenv.detect() == osenv.WINDOWS:
        try:
            instances = winterm.list_claude_instances(timeout=float(cfg.timeout))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return []
        out = []
        for inst in instances:
            cwd = inst[2] if len(inst) > 2 else ""
            if winterm.dir_skipped(cwd, cfg.skip_dirs):
                continue  # user-excluded: never counted, never fired at
            label = winterm.dir_label(cwd) or ("pid %s" % inst[1])
            out.append((label, limits.state_for_cwd(cwd) if cwd else limits.UNKNOWN))
        return out
    return [(winterm.dir_label(cwd) or cwd, state)
            for cwd, state in limits.recent_states(now=now)]


def snapshot(cfg: Config, now: Optional[datetime] = None) -> Snapshot:
    """Aggregate ``session_states`` into the counts the watch loop reasons about."""
    now = now or _utc_now()
    states = session_states(cfg, now)
    ready = [s for _l, s in states if s.resumable(now)]
    # `capped` first: a model cap is limited with a reset ahead, so the kind-blind
    # waiting() claims it, and the loop would then schedule around a reset it has no
    # business acting on.
    capped = [s for _l, s in states if s.capped]
    waiting = [s for _l, s in states if s.waiting(now) and not s.capped]
    resets = [s.reset_at for s in waiting if s.reset_at is not None]
    return Snapshot(
        known=any(s.known for _l, s in states),
        ready=len(ready),
        waiting=len(waiting),
        capped=len(capped),
        idle=len(states) - len(ready) - len(waiting) - len(capped),
        soonest=min(resets) if resets else None,
        detail=limits.summarise(states, now),
    )


def gate_instances(instances, *, now, state_fn=None):
    """Split live Windows sessions into ``(resumable, held)`` by limit state.

    ``resumable`` is the sessions Claude Code's transcript shows parked on a limit
    whose reset has arrived — the only ones a ``continue`` belongs in. ``held`` is
    ``[(instance, LimitState)]`` for everything else, so the caller can say *why*
    nothing fired instead of going quiet.

    A session whose state cannot be read (no transcript, unreadable working
    directory) is held, not fired at. Guessing is what produced the behaviour this
    gate exists to stop; the caller logs the miss so it never fails silently, and
    ``--no-require-limit`` remains the escape hatch. Pure apart from ``state_fn``,
    which is injectable for tests.
    """
    state_fn = state_fn or limits.state_for_cwd
    ready, held = [], []
    for inst in instances:
        cwd = inst[2] if len(inst) > 2 else ""
        state = state_fn(cwd) if cwd else limits.UNKNOWN
        if state.resumable(now):
            ready.append(inst)
        else:
            held.append((inst, state))
    return ready, held


def _held_note(held, now) -> str:
    """Human-readable reason each held session was left alone."""
    notes = []
    for inst, state in held:
        where = winterm.dir_label(inst[2] if len(inst) > 2 else "") or str(inst[1])
        if not state.known:
            notes.append("%s: no readable transcript" % where)
        elif state.kind == "fresh":
            notes.append("%s: cleared/new — no work to resume" % where)
        elif not state.limited:
            notes.append("%s: not limited" % where)
        elif state.kind == "model":
            notes.append("%s: model cap (continue won't clear it)" % where)
        elif state.stale(now):
            notes.append("%s: limit too old to act on" % where)
        elif state.reset_at is not None:
            notes.append("%s: limited until %s"
                         % (where, state.reset_at.astimezone().strftime("%H:%M")))
        else:
            notes.append("%s: limited" % where)
    return "; ".join(notes)


def _soonest_reset(held, now):
    """Earliest future reset among held sessions we would actually act on, or None.

    A capped session is skipped: its reset is when the model comes back, not a time
    this tool does anything at, and re-arming on it would wake the watcher for a
    session it cannot help.
    """
    times = [st.reset_at for _inst, st in held if st.waiting(now) and not st.capped]
    return min(times) if times else None


def _require_any_limit(cfg: Config, dry_run: bool, now=None) -> None:
    """Cross-platform gate for the paths with no per-session working directory.

    iTerm2 / tmux / single-window keystroke all broadcast without knowing which
    session is which, so the question narrows to "is ANY recently-active session
    parked on a spent limit?". Raises ``NothingToResume`` when the answer is no.
    """
    if not cfg.require_limit or dry_run:
        return  # a preview must describe the action, not refuse it
    now = now or _utc_now()
    snap = snapshot(cfg, now)
    if snap.ready:
        return
    raise NothingToResume(snap.detail, retry_at=snap.soonest)


def _resume(cfg: Config, dry_run: bool) -> list:
    plat = osenv.detect()
    # tmux is terminal-agnostic and works on macOS/Linux alike — check it first so
    # a non-iTerm2 (or Linux) user can opt in regardless of platform.
    if cfg.tmux:
        _require_any_limit(cfg, dry_run)
        return _broadcast_tmux(cfg, dry_run)
    # macOS resumes by broadcasting into iTerm2 (its keystroke equivalent).
    if plat == osenv.MACOS:
        _require_any_limit(cfg, dry_run)
        return _broadcast_iterm(cfg, dry_run)
    # Windows: continue EVERY running Claude session by writing `continue` straight
    # into each one's console input (AttachConsole + WriteConsoleInput). This is the
    # iTerm2-broadcast analogue — it resumes all sessions regardless of how they're
    # arranged (separate windows, tabs, or split panes in one Windows Terminal
    # window) without stealing focus. (Native Windows only; WSL's Claude is a Linux
    # process Win32_Process can't see, so it uses the single window_title path.)
    if cfg.keystroke_all and plat == osenv.WINDOWS:
        return _continue_all(cfg, dry_run)
    # Windows/WSL: keystroke into a single terminal window via PowerShell SendKeys.
    if cfg.keystroke and plat in (osenv.WINDOWS, osenv.WSL):
        _require_any_limit(cfg, dry_run)
        try:
            return winterm.send_keystroke(
                cfg.text, window_title=cfg.window_title, dry_run=dry_run, timeout=float(cfg.timeout)
            )
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise ActionError("keystroke send failed: %s" % e) from e
    raise ActionError(
        "no resume action for this platform (%s) — set --exec '<command>' for a "
        "headless run, or --tmux to resume Claude panes running inside tmux%s"
        % (plat, ", or --keystroke" if plat in (osenv.WINDOWS, osenv.WSL) else "")
    )


def _continue_all(cfg: Config, dry_run: bool) -> list:
    """Continue every running Claude session on Windows via console-input
    injection. No Claude running (empty list) is not an error — return [] so the
    loop re-arms, matching how an empty session list behaves on the other
    platforms.

    With ``require_limit`` on (the default) the live sessions are first narrowed to
    the ones actually parked on a spent limit, so a session that simply finished its
    work never receives a ``continue`` it did not ask for."""
    try:
        # None means "let continue_instances enumerate them" — the pre-gate path,
        # kept intact so turning the gate off costs nothing and lists once.
        instances = None
        if cfg.require_limit:
            instances = _gate(cfg, winterm.list_claude_instances(timeout=float(cfg.timeout)), dry_run)
        return winterm.continue_instances(cfg.text, instances=instances, dry_run=dry_run,
                                          timeout=float(cfg.timeout), skip_dirs=cfg.skip_dirs)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        raise ActionError("continue-all failed: %s" % e) from e


def _gate(cfg: Config, instances, dry_run: bool) -> list:
    """Apply the limit gate to a live instance list, logging what it held back."""
    now = _utc_now()
    ready, held = gate_instances(instances, now=now)
    if held and not dry_run:
        # INFO, not WARNING: holding a session back is the gate working. The GUI's
        # warning slot is for things that are broken, and a nightly "3 sessions were
        # busy" would drown the signals that matter.
        get_logger().info("limit gate: holding %d session(s) — %s",
                          len(held), _held_note(held, now))
    if ready or dry_run:
        return ready
    raise NothingToResume(_held_note(held, now) or "no Claude sessions running",
                          retry_at=_soonest_reset(held, now))


def _broadcast_tmux(cfg: Config, dry_run: bool) -> list:
    try:
        return tmux.broadcast(
            cfg.text,
            cfg.filter,
            skip_busy=cfg.skip_busy,
            session=cfg.session,
            dry_run=dry_run,
            all_sessions=cfg.all_sessions,
            force=cfg.force,
            busy_pattern=cfg.tmux_busy_pattern,
            timeout=float(cfg.timeout),
        )
    except (tmux.TmuxError, OSError, subprocess.SubprocessError) as e:
        raise ActionError("tmux send failed: %s" % e) from e


def _broadcast_iterm(cfg: Config, dry_run: bool) -> list:
    try:
        return iterm.broadcast(
            cfg.text,
            cfg.filter,
            skip_busy=cfg.skip_busy,
            session=cfg.session,
            dry_run=dry_run,
            all_sessions=cfg.all_sessions,
            force=cfg.force,
            timeout=float(cfg.timeout),
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        raise ActionError("iTerm2 broadcast failed: %s" % e) from e


def _run_exec(command: str, dry_run: bool = False, label: str = "exec") -> list:
    try:
        argv = osenv.split_command(command)
    except ValueError as e:
        raise ActionError("invalid %s command %r: %s" % (label, command, e)) from e
    if not argv:
        raise ActionError("%s command is empty" % label)

    label = "%s: %s" % (label, command)
    if dry_run:
        return [label]
    # Detach so the headless run outlives this process and doesn't tie up the
    # watch loop. resolve_argv handles Windows .cmd shims (claude.cmd etc.).
    try:
        subprocess.Popen(
            osenv.resolve_argv(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **osenv.detached_popen_kwargs(),
        )
    except OSError as e:
        raise ActionError("failed to launch exec command %r: %s" % (command, e)) from e
    return [label]
