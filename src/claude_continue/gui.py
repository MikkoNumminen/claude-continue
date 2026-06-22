"""A tiny Tkinter toggle window for the watch loop.

One button: Start watching / Stop watching. While it's on, a status line shows
the next reset and a live countdown. The watch runs only while the app is open
(closing the window stops it) — no agent is installed.

The window is a thin shell: it runs the same ``watch.run`` loop in a background
thread, with an interruptible stop Event so Stop is instant. ``WatchController``
holds all the logic and is Tk-free (and unit-tested); ``run()`` is the view.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__, ccusage, iterm, osenv, schedule, tmux, update, watch, winterm
from .action import ActionError
from .config import CONFIG_PATH, resolve
from .lock import AlreadyRunning

_MAX_SESSIONS_SHOWN = 8
# Poll iTerm faster while watching (status matters then), slower when idle to
# avoid spawning osascript every few seconds for the whole time the app is open.
_SESSION_POLL_WATCHING_MS = 5000
_SESSION_POLL_IDLE_MS = 15000
# Re-check for updates periodically (the app may stay open for days) and when the
# window regains focus, so the button turns green on its own when a release drops
# — debounced so focus storms / dialog closes don't hammer the releases API.
_UPDATE_RECHECK_MS = 6 * 60 * 60 * 1000  # every 6 hours
_UPDATE_MIN_AUTO_S = 120.0               # min seconds between auto re-checks


def _default_gui_log_path():
    """Where the GUI persists watch output — next to the JSON config, so a failed
    overnight run leaves a trail the user (or the doctor) can read afterward."""
    return CONFIG_PATH.parent / "gui.log"


class _FireTap(logging.Handler):
    """Forwards each watch log record to a callback (to count fires AND surface
    warnings — passes the record, not just the message, so the callback can see
    the level)."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def emit(self, record):
        try:
            self._callback(record)
        except Exception:  # noqa: BLE001 - a logging tap must never raise
            pass


class WatchController:
    """Start/stop the watch loop in a background thread. Tk-free, testable."""

    def __init__(self, runner=watch.run, log_path=None):
        self._runner = runner
        self._stop = threading.Event()
        self._stop_requested = False
        self._thread = None
        self._lock = threading.Lock()
        self._error = None
        self._fires = 0
        self._last_fired = None
        self._last_warning = None  # (datetime, message) of the latest watch warning
        # A per-instance Logger (not via getLogger) so multiple controllers don't
        # share handlers. propagate=False keeps watch logs out of the root logger.
        self._logger = logging.Logger("claude_continue.gui")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(_FireTap(self._on_log))
        # Persist watch output to a file so a failed overnight run leaves a trail.
        # The GUI watch otherwise logs nowhere (unlike the launchd/Task-Scheduler
        # agent, whose stdout is captured). Off by default so tests stay
        # side-effect-free; the real GUI passes _default_gui_log_path().
        if log_path is not None:
            self._add_file_handler(log_path)

    def _add_file_handler(self, log_path) -> None:
        try:
            from logging.handlers import RotatingFileHandler
            from pathlib import Path
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                str(path), maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            self._logger.addHandler(handler)
        except OSError:
            pass  # logging to a file is best-effort; never block watching

    # --- state queries ---
    def is_watching(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def is_stopping(self) -> bool:
        # stop was requested but the worker is still finishing an in-flight cycle
        # (an osascript/ccusage subprocess can't be interrupted by the Event)
        return self._stop_requested and self.is_watching()

    @property
    def error(self):
        return self._error

    @property
    def fires(self) -> int:
        return self._fires

    @property
    def last_fired(self):
        return self._last_fired

    @property
    def last_warning(self):
        """(datetime, message) of the most recent watch WARNING, or None. Cleared
        when a real fire lands. The UI shows it so a silently-failing watch (e.g.
        keystroke can't find its target window) doesn't just sit on 'WATCHING'."""
        return self._last_warning

    # --- control ---
    def start(self, cfg) -> None:
        with self._lock:
            if self.is_watching():
                return
            self._stop.clear()
            self._stop_requested = False
            self._error = None
            thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self._thread = thread
            thread.start()

    def request_stop(self) -> None:
        """Ask the watch to stop and return immediately. The worker is a daemon
        thread and exits at its next sleep/loop boundary — call this from the UI
        thread so the window never blocks on an in-flight fire."""
        self._stop_requested = True
        self._stop.set()

    def stop(self, timeout=5.0) -> None:
        """Request stop and wait up to ``timeout`` for the thread to exit. For
        use OFF the UI thread (tests, CLI); the GUI uses request_stop()."""
        self.request_stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- internals ---
    def _run(self, cfg) -> None:
        try:
            # stop=Event.is_set + sleep=Event.wait => Stop interrupts the sleep at once
            self._runner(
                cfg,
                logger=self._logger,
                stop=self._stop.is_set,
                sleep=self._stop.wait,
                use_lock=True,
            )
        except AlreadyRunning as e:
            self._error = str(e)
        except ActionError as e:
            self._error = str(e)
        except Exception as e:  # noqa: BLE001 - surface in the UI, never crash the app
            self._error = "watch stopped: %s" % e

    def _on_log(self, record) -> None:
        message = record.getMessage()
        if message.startswith("fired ->"):
            with self._lock:
                self._fires += 1
                self._last_fired = datetime.now()
                self._last_warning = None  # a real fire clears a prior failure note
        elif record.levelno >= logging.WARNING:
            # "fire failed", "gave up after N retries", "ccusage unavailable",
            # timing clamps — the signals that explain a watch that looks alive but
            # isn't resuming anything. watch._fire catches the keystroke
            # ActionError and logs it here; without this it went nowhere and the
            # failure was invisible.
            with self._lock:
                self._last_warning = (datetime.now(), message)


def format_sessions(sessions, note, *, watching, cfg) -> str:
    """Render the 'Claude instances' panel shown above the button.

    ``sessions`` is a list of (name, status) where status is "working" or "idle",
    or None when unavailable. When watching, each row is annotated with whether
    the watcher will affect it — mirroring the broadcast's skip-busy logic
    (idle sessions get a `continue`; busy ones are skipped).
    """
    if sessions is None:
        return "Claude instances: " + (note or "checking…")
    if not sessions:
        return "Claude instances: none found"

    exec_mode = bool(cfg.exec_cmd)
    effective_skip_busy = cfg.skip_busy and not cfg.force
    lines = ["Claude instances (%d):" % len(sessions)]
    for name, status in sessions[:_MAX_SESSIONS_SHOWN]:
        marker = "●" if status == "working" else "○"
        if watching:
            if exec_mode:
                affect = "(headless run)"
            elif (not effective_skip_busy) or status != "working":
                affect = "-> will resume"
            else:
                affect = "-- skipped (busy)"
            lines.append("  %s %-7s %-17s %s" % (marker, status, affect, name))
        else:
            lines.append("  %s %-7s %s" % (marker, status, name))
    if len(sessions) > _MAX_SESSIONS_SHOWN:
        lines.append("  ...and %d more" % (len(sessions) - _MAX_SESSIONS_SHOWN))
    return "\n".join(lines)


def effective_cfg(cfg):
    """The config the GUI actually runs with. Pure/testable.

    On Windows/WSL, when no resume action is configured, default to keystroke so
    "Continue terminals" works out of the box — the GUI is the zero-config entry
    point, the same way the Mac app's button just broadcasts to iTerm2. (The CLI
    keeps keystroke opt-in: a focus-stealing SendKeys is fine when a user clicks a
    button, but not as a silent default for an unattended `fire`/`watch`.)
    """
    if cfg.exec_cmd or cfg.tmux or cfg.keystroke or cfg.keystroke_all:
        return cfg  # an action is already configured — don't override it
    # Native Windows: continue EVERY running Claude session (the panel lists them)
    # by writing into each one's console input — the honest match for the macOS
    # broadcast, which resumes all sessions, not one window.
    if osenv.is_windows():
        return replace(cfg, keystroke=True, keystroke_all=True)
    # WSL: Claude runs as a Linux process Win32_Process can't enumerate, so the
    # tab-cycling can't see it — fall back to the single titled-window keystroke.
    if osenv.detect() == osenv.WSL:
        return replace(cfg, keystroke=True)
    return cfg


def win_instances_mode(cfg) -> bool:
    """True when the panel should list running Claude Code processes rather than
    iTerm2/tmux sessions. Scoped to native Windows: WSL's Claude runs as a Linux
    process that Win32_Process can't see, and macOS/Linux use the other paths."""
    return osenv.is_windows() and not cfg.tmux


def format_instances(instances, note, *, watching=False) -> str:
    """Render the Windows 'Claude instances' panel — the running Claude Code
    processes (``claude.exe`` / node CLI). Windows has no iTerm2-style
    "is processing" signal, so there's no working/idle marker; each is shown as
    running. ``instances`` is a list of (name, pid), or None when unavailable.

    While watching (continue-all mode), each row is annotated "-> will continue"
    so the panel and the action agree — the panel showing N instances now means
    all N get a `continue` at reset, closing the gap that made it look like the
    watcher ignored them."""
    if instances is None:
        return "Claude instances: " + (note or "checking…")
    if not instances:
        return "Claude instances: none running"
    lines = ["Claude instances (%d):" % len(instances)]
    for name, pid in instances[:_MAX_SESSIONS_SHOWN]:
        # native install lists as "claude"; an npm node CLI lists as "claude (node)"
        # so it still reads as a Claude instance, not a stray node process.
        label = name if name == "claude" else "claude (%s)" % name
        if watching:
            lines.append("  ● %-13s %-15s (pid %s)" % (label, "-> will continue", pid))
        else:
            lines.append("  ● %-13s (pid %s)" % (label, pid))
    if len(instances) > _MAX_SESSIONS_SHOWN:
        lines.append("  ...and %d more" % (len(instances) - _MAX_SESSIONS_SHOWN))
    return "\n".join(lines)


def update_decision(info, *, frozen):
    """Pure decision for the 'checked' phase. Returns (kind, message) where kind
    is 'prompt' (offer to download+restart) or 'none' (just show the message).
    Kept side-effect-free so it's unit-testable without Tk."""
    if info is None or info.error:
        return "none", "update check failed: %s" % (info.error if info else "no data")
    if not info.newer:
        return "none", "up to date (v%s)" % info.current
    # info.latest is the tag (already "vX.Y.Z"); info.current is bare (e.g. "0.3.0").
    if not frozen:
        return "none", "%s available — update from source with `git pull`" % info.latest
    if not info.asset_url:
        return "none", "%s available, but no build for this platform" % info.latest
    return "prompt", "%s available" % info.latest


_BTN_UPDATE_AVAILABLE = "#1a7f37"  # green: an installable update is waiting
_BTN_UP_TO_DATE = "#888888"        # gray: current / nothing to install
# (readable as TEXT colour — we tint the button's text + the status line, never
# the button background: a coloured highlightbackground paints an ugly box on the
# macOS native button.)


def should_auto_recheck(last_check, now, *, min_interval=_UPDATE_MIN_AUTO_S):
    """Debounce automatic update re-checks: True only if enough time has passed
    since the last one (so a focus storm or a closing dialog can't hammer the
    releases API). ``last_check`` is None before the first check. Pure/testable."""
    if last_check is None:
        return True
    return (now - last_check) >= min_interval


_NOTE_WARN = "#a00"
_NOTE_OK = "#2a2"


def watching_note(last_warning, last_fired, fires):
    """The status note shown while watching, as (text, color). The latest warning
    wins over the 'last fired' confirmation when it's at least as recent — a failed
    fire isn't fatal (the loop keeps retrying), but the user must SEE it, else
    "WATCHING" looks fine while nothing is resuming. ('', None) when there's nothing
    to show. Pure so the precedence is testable (it used to live inline in run())."""
    if last_warning is not None and (last_fired is None or last_warning[0] >= last_fired):
        return ("⚠ %s" % last_warning[1], _NOTE_WARN)
    if last_fired is not None:
        return ("last fired %s ✓  (%d total)" % (last_fired.strftime("%H:%M"), fires), _NOTE_OK)
    return ("", None)


def should_annotate_continue(watching, quota, keystroke_all):
    """Whether the Windows instances panel should mark each row '-> will continue':
    only while actively continuing every session (continue-all), never in quota
    mode (which just opens a window and doesn't touch the listed PIDs). Pure."""
    return bool(watching and not quota and keystroke_all)


def offset_from_clock(raw_reset, hh: int, mm: int) -> int:
    """Seconds to add to the ccusage estimate ``raw_reset`` (tz-aware UTC) so firing
    lands on the local wall-clock time HH:MM the user typed in the "Fire at" field.

    Picks the HH:MM occurrence NEAREST the estimate (within ±1 day), so a late-evening
    estimate corrected to an after-midnight time resolves to "20 min later", not "23h
    earlier" — the flooring error is under an hour, so nearest is unambiguous. Pure
    and testable. Returns a signed second count (negative if the real reset is earlier
    than the estimate).

    DST-correct: each candidate is built as a NAIVE local wall-clock time and localized
    with ``.astimezone()`` (which reads the OS zone, DST and all), so a target on the
    far side of a spring-forward / fall-back seam gets the offset actually in effect at
    that wall-clock time — not the estimate's offset. (A plain ``.replace(hour=...)`` on
    the estimate would pin the wrong offset and schedule the fire up to an hour off.)"""
    naive = raw_reset.astimezone().replace(hour=hh, minute=mm, second=0, microsecond=0, tzinfo=None)
    candidates = [(naive + timedelta(days=d)).astimezone() for d in (-1, 0, 1)]
    best = min(candidates, key=lambda c: abs((c - raw_reset).total_seconds()))
    return int(round((best - raw_reset).total_seconds()))


def format_reset_field(raw_reset, offset_seconds: int):
    """Render the GUI "Fire at" control as ``(entry_text, hint_text)``.

    ``entry_text`` is the corrected fire time (estimate + offset) as local HH:MM;
    ``hint_text`` explains what's applied. ``('', 'waiting…')`` when there's no
    estimate yet (idle / ccusage down). Pure and testable."""
    if raw_reset is None:
        return ("", "waiting for an active window…")
    local = raw_reset.astimezone()  # the raw estimate, local — for the hint text
    # Add the offset to the UTC INSTANT, then re-localize — mirroring offset_from_clock.
    # Adding to `local` (a fixed-offset datetime) would keep the pre-seam offset and
    # render the wrong wall-clock across a DST transition (the inverse asymmetry).
    corrected = (raw_reset + timedelta(seconds=offset_seconds)).astimezone()
    entry = corrected.strftime("%H:%M")
    mins = int(round(offset_seconds / 60.0))
    if mins == 0:
        # 0, or a sub-minute correction that rounds to 0 — the entry shows the same
        # HH:MM as the estimate, so read it as "on the estimate", not "+0m applied".
        return (entry, "auto-estimate (resets %s) — edit if it's landing wrong" % local.strftime("%H:%M"))
    return (entry, "estimate %s, %+dm correction applied to every reset" % (local.strftime("%H:%M"), mins))


def parse_reset_input(raw_reset, text: str):
    """Pure core of the GUI "Fire at" commit: turn the typed text into a correction.

    Returns ``(offset_seconds, error)``:
    - ``(None, None)``  — no change to apply: no estimate yet, OR an empty field. An
      empty field is a no-op (NOT a clear), so an accidental blur/alt-tab mid-edit
      can't silently wipe a good correction — clearing is the explicit "use estimate".
    - ``(secs, None)``  — a valid HH:MM, parsed to a signed offset vs the estimate.
    - ``(None, msg)``   — invalid input; ``msg`` is the error to show.

    Kept Tk-free so the parse/validate branching is unit-testable (the widget glue
    in ``run()`` is not)."""
    if raw_reset is None or not text.strip():
        return (None, None)
    try:
        hh, mm = schedule.parse_hhmm(text)
    except ValueError:
        return (None, "enter a 24-hour time like 17:42")
    return (offset_from_clock(raw_reset, hh, mm), None)


def reset_controls_state(*, watching: bool, has_estimate: bool, offset: int):
    """``(entry_enabled, estimate_btn_enabled)`` for the "Fire at" controls. The field
    locks while a watch runs (settings apply at start) or before an estimate exists to
    correct against; the "use estimate" button is dead when already on the estimate
    (offset 0). Pure, so the lock logic is unit-testable apart from the Tk glue."""
    entry_enabled = not watching and has_estimate
    btn_enabled = not watching and offset != 0
    return (entry_enabled, btn_enabled)


def update_button_color(info, *, frozen):
    """Tint for the Update button: green when an installable update is available,
    gray when up-to-date (or not installable), None when unknown (no check yet /
    error) so the caller leaves the default."""
    if info is None or info.error:
        return None
    kind, _ = update_decision(info, frozen=frozen)
    return _BTN_UPDATE_AVAILABLE if kind == "prompt" else _BTN_UP_TO_DATE


def update_button_label(info, *, frozen):
    """Button text carrying a COLOUR GLYPH, because macOS's native Tk button
    ignores fg/bg — an emoji dot is the only colour that reliably renders there.
    🟢 = an installable update is waiting; ✓ = up to date; ⟳ = unknown/checking."""
    if info is not None and not info.error:
        kind, _ = update_decision(info, frozen=frozen)
        if kind == "prompt":
            return "🟢  Update"
        if not info.newer:
            return "✓  Up to date"
    return "⟳  Update"


def watch_explanation(cfg) -> str:
    """Plain-language description of what 'Start watching' will do, given the
    config. Shown in the idle state so the user knows the effect before clicking.
    Pure (no Tk) so it's unit-testable."""
    when = "When you start watching, claude-continue waits for your Claude usage window to reset, then "
    if cfg.start_window:
        return when + ("opens a fresh window headlessly (no terminals touched) — keeping your "
                       "5-hour windows back-to-back. It opens one right away if you have none.")
    if cfg.exec_cmd:
        return when + ("runs `%s` headlessly — so work resumes the instant your quota refreshes." % cfg.exec_cmd)
    # Windows "continue all": writes `continue` straight into each Claude process's
    # console input (no focus, any tab/pane/window). tmux wins over it (action._resume).
    if cfg.keystroke_all and not cfg.tmux and osenv.is_windows():
        return when + ('sends “%s” to every running Claude session — it writes it straight into '
                       'each one’s input, so it works whether they’re separate windows, tabs, or '
                       'split panes, without stealing focus.' % cfg.text)
    # --keystroke is the Windows/WSL path: it types into a single titled window
    # (no session/skip-busy concept). tmux wins over it (matches action._resume),
    # and on macOS keystroke is a no-op that falls through to the iTerm2 broadcast.
    if cfg.keystroke and not cfg.tmux and osenv.detect() in (osenv.WINDOWS, osenv.WSL):
        return when + ('types “%s” into the “%s” window — so paused work resumes the instant your quota refreshes.'
                       % (cfg.text, cfg.window_title))
    if cfg.session:
        target = "the “%s” session" % cfg.session
    elif cfg.tmux:
        target = "Claude panes running in tmux"
    else:
        target = "idle Claude sessions in iTerm2"
    body = ('sends “%s” to %s — so paused work resumes the instant your quota refreshes. ' % (cfg.text, target))
    if cfg.force:
        body += "Busy sessions are nudged too (force is on)."
    elif cfg.skip_busy:
        body += "Busy sessions are skipped; only idle ones are nudged."
    else:
        body += "All matched sessions are nudged (skip-busy is off)."
    return when + body


def run(stale_warning: str | None = None) -> None:  # pragma: no cover - exercised manually; logic lives in WatchController
    """Open the toggle window. Imports tkinter lazily so the rest of the package
    doesn't require a display.

    ``stale_warning`` (if set) is shown in a Tk dialog once the window is up. It
    can only originate from a frozen Windows build, which is windowed (no console),
    so printing it to stdout would silently vanish — the GUI is the only sink the
    user will actually see."""
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox

    controller = WatchController(log_path=_default_gui_log_path())
    # Config is snapshotted once at startup; edits to the config file / env take
    # effect on the next launch, not mid-session. effective_cfg defaults Windows/WSL
    # to keystroke (so "Continue terminals" works zero-config) and is applied once
    # here, so the pre-watch explanation and the keystroke action agree. (The
    # instances panel is an independent view of running processes, not the action.)
    app_cfg = effective_cfg(resolve())
    # heterogeneous UI state bags mutated by worker threads, read on the main thread
    poll: dict[str, Any] = {"reset_at": None, "note": "", "busy": False,
                            "sessions": None, "sessions_note": "", "sessions_busy": False}
    # self-update state machine: idle -> checking -> checked -> [applying -> done] / error
    # `auto` marks a background (startup) check that colours the button without prompting.
    upd: dict[str, Any] = {"phase": "idle", "info": None, "msg": "", "error": None, "auto": False}
    # self-removal state: idle -> removing -> done (quit; the helper deletes the app) / error
    rem: dict[str, Any] = {"phase": "idle", "error": None}
    # which mode the running watch is in, so the right button shows "Stop"; "offset"
    # is the correction the running worker was started with — the countdown reads this
    # snapshot (not the live field), so a mid-watch edit can't make the label lie.
    watch_mode: dict[str, Any] = {"quota": False, "offset": 0}
    # user override of the fire time: a signed second correction to ccusage's reset
    # estimate (the "Fire at" field). Seeded from config (rounded to whole minutes —
    # the field is minute-granular) so a CLI/env value pre-fills it; a junk config
    # value degrades to 0 rather than crashing the zero-config GUI at startup. "bad"
    # holds a pending invalid-input note so refresh() doesn't wipe it.
    try:
        _seed_offset = int(round(float(app_cfg.reset_offset) / 60.0)) * 60
    except (TypeError, ValueError):
        _seed_offset = 0
    override: dict[str, Any] = {"offset": _seed_offset, "bad": False}

    root = tk.Tk()
    root.title("claude-continue")
    root.geometry("470x540")
    root.resizable(True, True)
    root.minsize(440, 440)

    if stale_warning:
        # defer so the main window paints first, then surface the one-line warning
        root.after(200, lambda: messagebox.showwarning("Update incomplete", stale_warning, parent=root))

    dot = tk.Label(root, text="○", font=tkfont.Font(size=30))
    dot.pack(pady=(18, 0))
    status = tk.Label(root, text="Idle", font=tkfont.Font(size=15, weight="bold"))
    status.pack()
    detail = tk.Label(root, text="press Start to watch the quota", fg="#666")
    detail.pack(pady=(2, 10))
    sessions_label = tk.Label(root, text="Claude instances: checking…",
                              font="TkFixedFont", justify="left", anchor="w", wraplength=440)
    sessions_label.pack(fill="x", padx=16, pady=(0, 10))
    explain = tk.Label(root, text="", fg="#555", wraplength=430, justify="center")
    explain.pack(padx=16, pady=(0, 10))
    # "Fire at" row: the reset time both buttons act on. Pre-filled with ccusage's
    # estimate; edit it when the estimate is landing wrong and the gap is reused on
    # every later window (see offset_from_clock / format_reset_field).
    reset_frame = tk.Frame(root)
    reset_frame.pack()
    tk.Label(reset_frame, text="Fire at:").pack(side="left")
    reset_entry = tk.Entry(reset_frame, width=6, justify="center")
    reset_entry.pack(side="left", padx=(4, 6))
    reset_estimate_btn = tk.Button(reset_frame, text="use estimate", fg="#36c", borderwidth=0,
                                   highlightthickness=0, font=tkfont.Font(size=10))
    reset_estimate_btn.pack(side="left")
    reset_hint = tk.Label(root, text="", fg="#777", wraplength=420, font=tkfont.Font(size=10))
    reset_hint.pack(pady=(0, 8))
    button = tk.Button(root, text="▶  Continue terminals", width=24, height=2)
    button.pack()
    quota_button = tk.Button(root, text="＋  Start quota", width=24)
    quota_button.pack(pady=(6, 0))
    note = tk.Label(root, text="", fg="#a00", wraplength=420)
    note.pack(pady=(8, 0))
    # bottom row: a low-key "Remove…" link sits under the Update button
    remove_button = tk.Button(root, text="Remove app…", fg="#a00", borderwidth=0,
                              highlightthickness=0, font=tkfont.Font(size=11))
    remove_button.pack(side="bottom", pady=(0, 8))
    update_button = tk.Button(root, text="⟳  Update", width=14)
    update_button.pack(side="bottom", pady=(0, 8))
    update_status = tk.Label(root, text="", fg="#666", wraplength=430)
    update_status.pack(side="bottom")

    def poll_ccusage():
        if poll["busy"]:
            return
        poll["busy"] = True

        def work():
            try:
                block = ccusage.get_active_block()
                poll["reset_at"] = block.reset_at if block else None
                poll["note"] = "" if block else "no active window yet"
            except Exception:  # noqa: BLE001
                poll["reset_at"] = None
                poll["note"] = "ccusage unavailable"
            finally:
                poll["busy"] = False

        threading.Thread(target=work, daemon=True).start()

    def poll_sessions():
        if poll["sessions_busy"]:
            return
        poll["sessions_busy"] = True

        def work():
            try:
                if app_cfg.tmux:  # terminal-agnostic; works on any platform
                    poll["sessions"] = tmux.list_sessions(
                        app_cfg.filter, session=app_cfg.session,
                        all_sessions=app_cfg.all_sessions,
                        busy_pattern=app_cfg.tmux_busy_pattern, timeout=float(app_cfg.timeout),
                    )
                    poll["sessions_note"] = ""
                elif osenv.is_macos():
                    poll["sessions"] = iterm.list_sessions(
                        app_cfg.filter, session=app_cfg.session,
                        all_sessions=app_cfg.all_sessions, timeout=float(app_cfg.timeout),
                    )
                    poll["sessions_note"] = ""
                elif win_instances_mode(app_cfg):  # native Windows: list Claude processes
                    poll["sessions"] = winterm.list_claude_instances(timeout=float(app_cfg.timeout))
                    poll["sessions_note"] = ""
                else:
                    poll["sessions"] = None
                    # WSL keystroke / Linux / headless --exec: no listable processes
                    # here (WSL's Claude is a Linux process Win32_Process can't see).
                    poll["sessions_note"] = "no live process view on this platform"
            except Exception as e:  # noqa: BLE001
                poll["sessions"] = None
                if app_cfg.tmux:
                    src = "tmux"
                elif win_instances_mode(app_cfg):
                    src = "instance list"
                else:
                    src = "iTerm2"
                poll["sessions_note"] = "%s query failed: %s" % (src, str(e)[:50])
            finally:
                poll["sessions_busy"] = False

        threading.Thread(target=work, daemon=True).start()

    def countdown_text():
        if not controller.is_watching():
            return "stopped"
        if poll["note"]:
            return "watching · " + poll["note"]
        reset_at = poll["reset_at"]
        if reset_at is None:
            return "watching…"
        # show the CORRECTED reset (estimate + the offset this watch was STARTED with —
        # watch_mode["offset"], a snapshot, not the live field), so the countdown always
        # matches when the worker will actually fire even if the field is edited.
        corrected = reset_at + timedelta(seconds=watch_mode["offset"])
        secs = max(0, int((corrected - datetime.now(timezone.utc)).total_seconds()))
        hours, mins = divmod(secs // 60, 60)
        tag = " (corrected)" if watch_mode["offset"] else ""
        return "next reset %s%s · in %dh %02dm" % (corrected.astimezone().strftime("%H:%M"), tag, hours, mins)

    def set_buttons(active, stopping=False):
        # active: None (idle/error), "continue", or "quota". The active mode's
        # button becomes Stop; the other is disabled while a watch runs.
        if active is None:
            button.config(text="▶  Continue terminals", state="normal")
            quota_button.config(text="＋  Start quota", state="normal")
        elif active == "continue":
            button.config(text="Stopping…" if stopping else "⏹  Stop", state="disabled" if stopping else "normal")
            quota_button.config(text="＋  Start quota", state="disabled")
        else:  # quota
            quota_button.config(text="Stopping…" if stopping else "⏹  Stop", state="disabled" if stopping else "normal")
            button.config(text="▶  Continue terminals", state="disabled")

    def render_reset_field():
        # Repaint the "Fire at" entry/hint from the live estimate + current offset.
        # Skipped while the user is typing (don't stomp the field) or while an invalid
        # value is pending (leave the red hint up until they fix it or reset).
        if override["bad"] or root.focus_get() is reset_entry:
            return
        entry_text, hint_text = format_reset_field(poll["reset_at"], override["offset"])
        if reset_entry.get() != entry_text:
            reset_entry.config(state="normal")  # an Entry must be enabled to edit it
            reset_entry.delete(0, "end")
            reset_entry.insert(0, entry_text)
        reset_hint.config(text=hint_text, fg="#777")
        # Lock the field while a watch runs (settings apply at start) or before an
        # estimate exists (nothing to correct against yet) — pure decision in
        # reset_controls_state so it's unit-tested apart from this Tk glue.
        watching = controller.is_watching() or controller.is_stopping()
        entry_enabled, btn_enabled = reset_controls_state(
            watching=watching, has_estimate=poll["reset_at"] is not None, offset=override["offset"])
        reset_entry.config(state="normal" if entry_enabled else "disabled")
        reset_estimate_btn.config(state="normal" if btn_enabled else "disabled")

    def commit_reset_time(*_):
        # Parse the typed time into a signed offset vs the current estimate (pure
        # logic in parse_reset_input). Invalid input flags "bad" so the red hint
        # survives the next refresh; a valid value (or "use estimate") clears it.
        if controller.is_watching() or controller.is_stopping():
            return  # settings are locked while watching; ignore a stray late commit
        offset, error = parse_reset_input(poll["reset_at"], reset_entry.get())
        if error is not None:
            override["bad"] = True
            reset_hint.config(text=error, fg="#a00")
            return
        if offset is None:
            return  # no estimate to correct against yet — leave the field as-is
        override["offset"] = offset
        override["bad"] = False
        render_reset_field()

    def use_estimate():
        override["offset"] = 0
        override["bad"] = False
        render_reset_field()

    def commit_on_return(_e):
        commit_reset_time()
        if not override["bad"]:
            root.focus_set()  # leave the field so render repaints the canonical hint
        return "break"        # don't ring the bell / insert a newline

    reset_entry.bind("<Return>", commit_on_return)
    reset_entry.bind("<FocusOut>", commit_reset_time)
    reset_estimate_btn.config(command=use_estimate)

    def refresh():
        watching, stopping = controller.is_watching(), controller.is_stopping()
        mode = "quota" if watch_mode["quota"] else "continue"
        # the pre-watch explanation is only relevant before you start
        explain.config(text="" if watching or stopping else watch_explanation(app_cfg))
        if controller.error:
            dot.config(text="⚠", fg="#a00")
            status.config(text="Stopped")
            detail.config(text="")
            note.config(text=controller.error)
            set_buttons(None)
        elif stopping:
            dot.config(text="◐", fg="#c80")
            status.config(text="Stopping…")
            detail.config(text="finishing the current cycle")
            note.config(text="")
            set_buttons(mode, stopping=True)
        elif watching:
            dot.config(text="●", fg="#22aa22")
            status.config(text="WATCHING · quota" if watch_mode["quota"] else "WATCHING")
            detail.config(text=countdown_text())
            set_buttons(mode)
            text, color = watching_note(controller.last_warning, controller.last_fired, controller.fires)
            if color:
                note.config(text=text, fg=color)
            else:
                note.config(text=text)
        else:
            dot.config(text="○", fg="#999")
            status.config(text="Idle")
            detail.config(text="resume terminals at each reset, or just keep a window open")
            note.config(text="")
            set_buttons(None)
        if win_instances_mode(app_cfg):
            live = should_annotate_continue(watching, watch_mode["quota"], app_cfg.keystroke_all)
            sessions_label.config(text=format_instances(poll["sessions"], poll["sessions_note"], watching=live))
        else:
            live = watching and not watch_mode["quota"]
            sessions_label.config(text=format_sessions(
                poll["sessions"], poll["sessions_note"], watching=live, cfg=app_cfg))
        render_reset_field()
        root.after(1000, refresh)

    def start_watch(quota):
        if controller.is_watching() or controller.is_stopping():
            return
        # Starting commits to the last good "Fire at" value; clear any stale
        # invalid-input flag so the field shows the offset the watch actually uses.
        override["bad"] = False
        # "Start quota" must open a window even if exec_cmd is configured (exec
        # otherwise wins in action.perform); "Continue terminals" keeps exec_cmd.
        # reset_offset applies the user's reset-time correction to both buttons.
        cfg = (replace(app_cfg, start_window=True, exec_cmd=None, reset_offset=override["offset"])
               if quota else
               replace(app_cfg, start_window=False, reset_offset=override["offset"]))
        try:
            from . import action
            action.perform(cfg, dry_run=True)  # validate up front; fail clearly
        except ActionError as e:
            note.config(text=str(e), fg="#a00")
            return
        watch_mode["quota"] = quota
        watch_mode["offset"] = override["offset"]  # snapshot the offset the worker runs with
        controller.start(cfg)
        render_reset_field()  # lock the field NOW, not on the next refresh tick (~1s later)
        poll_ccusage()

    def toggle_continue():
        if controller.is_watching() or controller.is_stopping():
            if not watch_mode["quota"]:
                controller.request_stop()  # non-blocking
        else:
            start_watch(quota=False)

    def toggle_quota():
        if controller.is_watching() or controller.is_stopping():
            if watch_mode["quota"]:
                controller.request_stop()
        else:
            start_watch(quota=True)

    button.config(command=toggle_continue)
    quota_button.config(command=toggle_quota)

    def check_for_update(auto=False):
        if upd["phase"] in ("checking", "applying"):
            return
        if auto:
            # debounce periodic/focus re-checks; a manual click is never debounced
            if not should_auto_recheck(upd.get("last_auto"), time.monotonic()):
                return
            upd["last_auto"] = time.monotonic()
        upd["phase"] = "checking"
        upd["info"] = None
        upd["error"] = None
        upd["auto"] = auto
        upd["msg"] = "" if auto else "checking for updates…"

        def work():
            try:
                upd["info"] = update.check()
            except Exception as e:  # noqa: BLE001 - check() shouldn't raise, but never wedge the UI
                upd["info"] = update.UpdateInfo(__version__, None, False, None, None, error=str(e))
            upd["phase"] = "checked"

        threading.Thread(target=work, daemon=True).start()

    def _start_apply(info):
        upd["phase"] = "applying"
        upd["msg"] = "downloading %s…" % info.latest

        def work():
            try:
                update.apply_update(info)
                upd["phase"] = "done"
            except Exception as e:  # noqa: BLE001 - surfaced in the UI
                upd["error"] = str(e)
                upd["phase"] = "error"

        threading.Thread(target=work, daemon=True).start()

    def remove_app():
        if rem["phase"] == "removing":
            return
        if not messagebox.askyesno(
            "Remove claude-continue",
            "Remove claude-continue completely?\n\n"
            "This stops watching, removes the background agent, deletes your "
            "settings and logs, and deletes the app itself.\n\nThis cannot be undone.",
            icon="warning",
        ):
            return
        controller.request_stop()
        rem["phase"] = "removing"

        def work():
            try:
                from . import selfremove
                errs = []
                summary = selfremove.remove(
                    purge_config=True,
                    logger=lambda *a: errs.append(a[0] % a[1:] if len(a) > 1 else a[0]),
                )
                # Frozen but the bundle wasn't actually scheduled for deletion
                # (couldn't locate it, or the helper failed to spawn): config and
                # the agent are gone but the app survives — surface that, don't
                # report a clean "done" and quit.
                if summary["frozen"] and not summary["bundle_scheduled"]:
                    where = ("\n%s" % summary["bundle"]) if summary["bundle"] else ""
                    rem["error"] = ("Removed your settings and the background agent, but couldn't "
                                    "delete the app itself — please delete it manually." + where)
                    rem["phase"] = "error"
                else:
                    rem["phase"] = "done"
            except Exception as e:  # noqa: BLE001 - surfaced in the UI
                rem["error"] = str(e)
                rem["phase"] = "error"

        threading.Thread(target=work, daemon=True).start()

    remove_button.config(command=remove_app)

    def update_poll():
        # main-thread state machine: workers only mutate `upd`/`rem`; the dialogs
        # and quit all happen here so Tk is only touched on this thread.
        if rem["phase"] == "removing":
            update_status.config(text="removing…", fg="#a00")
            remove_button.config(state="disabled")
            update_button.config(state="disabled")
            root.after(500, update_poll)
            return
        if rem["phase"] == "done":
            # config/agent already gone; the detached helper deletes the bundle
            # once we exit. Quit promptly so it can run.
            root.destroy()
            return
        if rem["phase"] == "error":
            # deliver the failure via a modal so the update poll can't clobber it;
            # re-enable BOTH buttons (both were disabled during "removing").
            rem["phase"] = "idle"
            remove_button.config(state="normal")
            update_button.config(state="normal")
            messagebox.showerror("Remove failed", rem["error"] or "removal failed")
        phase = upd["phase"]
        if phase == "checked":
            info = upd["info"]
            kind, msg = update_decision(info, frozen=update.is_frozen())
            auto = upd.get("auto", False)
            if kind == "prompt" and not auto:
                upd["phase"] = "prompting"
                if messagebox.askyesno(
                    "Update available",
                    "%s is available (you have v%s).\nDownload it and restart now?" % (info.latest, info.current),
                ):
                    _start_apply(info)
                else:
                    upd["msg"] = "update postponed"
                    upd["phase"] = "idle"
            else:
                # a startup auto-check (or any non-installable result) just reports
                # status + colours the button; it never pops a dialog.
                upd["msg"] = ("%s available — click ⟳ to update" % info.latest) if kind == "prompt" else msg
                upd["phase"] = "idle"
        elif phase == "done":
            upd["msg"] = "updated — restarting…"
            upd["phase"] = "quitting"
            controller.request_stop()
            root.after(800, root.destroy)  # the new version was already launched
        elif phase == "error":
            upd["msg"] = "update failed: %s" % (upd["error"] or "")
            upd["phase"] = "idle"

        busy = upd["phase"] in ("checking", "applying", "prompting", "quitting")
        update_button.config(state="disabled" if busy else "normal")
        update_status.config(text=upd["msg"])
        # green when an update is installable, gray when up to date (None until the
        # first check completes). Tint the button TEXT + the status line — never the
        # background (a coloured highlightbackground is an ugly box on macOS).
        # macOS native buttons ignore fg/bg, so the visible signal is a colour
        # GLYPH in the button label (🟢/✓); also set fg (works on Linux/Windows)
        # and colour the status line (Labels honour fg on every platform).
        frozen = update.is_frozen()
        update_button.config(text=update_button_label(upd["info"], frozen=frozen))
        color = update_button_color(upd["info"], frozen=frozen)
        if color:
            update_button.config(fg=color)
            update_status.config(fg=color)
        root.after(500, update_poll)

    update_button.config(command=check_for_update)

    def update_recheck_loop():
        check_for_update(auto=True)  # debounced; colours the button if a release appeared
        root.after(_UPDATE_RECHECK_MS, update_recheck_loop)

    # re-check when the window regains focus, so it refreshes the moment you look
    # (update check is debounced; poll_ccusage's busy-guard makes a refresh cheap).
    # The ccusage poll keeps the idle "Fire at" estimate current before you start.
    def on_focus_in(_e):
        check_for_update(auto=True)
        poll_ccusage()
    root.bind("<FocusIn>", on_focus_in)

    def poll_loop():
        if controller.is_watching():
            poll_ccusage()
        root.after(30000, poll_loop)

    def on_close():
        # non-blocking: the worker is a daemon thread, so it won't keep the
        # process alive; don't join on the UI thread.
        controller.request_stop()
        root.destroy()

    def sessions_loop():
        poll_sessions()
        interval = _SESSION_POLL_WATCHING_MS if controller.is_watching() else _SESSION_POLL_IDLE_MS
        root.after(interval, sessions_loop)

    root.protocol("WM_DELETE_WINDOW", on_close)
    poll_sessions()  # populate the instances panel immediately
    poll_ccusage()   # fetch the reset estimate so "Fire at" pre-fills before watching
    refresh()
    root.after(30000, poll_loop)
    root.after(_SESSION_POLL_IDLE_MS, sessions_loop)
    root.after(500, update_poll)
    root.after(900, lambda: check_for_update(auto=True))  # colour the button on launch
    root.after(_UPDATE_RECHECK_MS, update_recheck_loop)   # then re-check periodically
    root.mainloop()
