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
from datetime import datetime, timezone
from typing import Any

from . import __version__, ccusage, iterm, osenv, tmux, update, watch, winterm
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
    # Native Windows: continue EVERY running Claude session (the panel lists them),
    # cycling a terminal's tabs — the honest match for the macOS broadcast, which
    # resumes all sessions, not one window.
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


def run() -> None:  # pragma: no cover - exercised manually; logic lives in WatchController
    """Open the toggle window. Imports tkinter lazily so the rest of the package
    doesn't require a display."""
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
    # which mode the running watch is in, so the right button shows "Stop"
    watch_mode: dict[str, Any] = {"quota": False}

    root = tk.Tk()
    root.title("claude-continue")
    root.geometry("470x540")
    root.resizable(True, True)
    root.minsize(440, 440)

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
        secs = max(0, int((reset_at - datetime.now(timezone.utc)).total_seconds()))
        hours, mins = divmod(secs // 60, 60)
        return "next reset %s · in %dh %02dm" % (reset_at.astimezone().strftime("%H:%M"), hours, mins)

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
            warn = controller.last_warning
            fired = controller.last_fired
            if warn is not None and (fired is None or warn[0] >= fired):
                # a failed/abandoned fire isn't fatal (the loop keeps retrying),
                # but the user must SEE it — otherwise "WATCHING" looks fine while
                # nothing is actually resuming.
                note.config(text="⚠ %s" % warn[1], fg="#a00")
            elif fired is not None:
                note.config(text="last fired %s ✓  (%d total)" % (fired.strftime("%H:%M"), controller.fires), fg="#2a2")
            else:
                note.config(text="")
        else:
            dot.config(text="○", fg="#999")
            status.config(text="Idle")
            detail.config(text="resume terminals at each reset, or just keep a window open")
            note.config(text="")
            set_buttons(None)
        if win_instances_mode(app_cfg):
            # continue-all resumes every listed instance; quota mode just opens a
            # window, so only annotate when actually continuing.
            live = watching and not watch_mode["quota"] and app_cfg.keystroke_all
            sessions_label.config(text=format_instances(poll["sessions"], poll["sessions_note"], watching=live))
        else:
            live = watching and not watch_mode["quota"]
            sessions_label.config(text=format_sessions(
                poll["sessions"], poll["sessions_note"], watching=live, cfg=app_cfg))
        root.after(1000, refresh)

    def start_watch(quota):
        if controller.is_watching() or controller.is_stopping():
            return
        # "Start quota" must open a window even if exec_cmd is configured (exec
        # otherwise wins in action.perform); "Continue terminals" keeps exec_cmd.
        cfg = replace(app_cfg, start_window=True, exec_cmd=None) if quota else replace(app_cfg, start_window=False)
        try:
            from . import action
            action.perform(cfg, dry_run=True)  # validate up front; fail clearly
        except ActionError as e:
            note.config(text=str(e), fg="#a00")
            return
        watch_mode["quota"] = quota
        controller.start(cfg)
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
    # (debounced inside check_for_update, so dialog closes / focus storms are cheap)
    root.bind("<FocusIn>", lambda e: check_for_update(auto=True))

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
    refresh()
    root.after(30000, poll_loop)
    root.after(_SESSION_POLL_IDLE_MS, sessions_loop)
    root.after(500, update_poll)
    root.after(900, lambda: check_for_update(auto=True))  # colour the button on launch
    root.after(_UPDATE_RECHECK_MS, update_recheck_loop)   # then re-check periodically
    root.mainloop()
