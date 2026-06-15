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
from datetime import datetime, timezone

from . import __version__, ccusage, iterm, osenv, tmux, update, watch
from .action import ActionError
from .config import resolve
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


class _FireTap(logging.Handler):
    """Forwards each watch log line to a callback (to count fires)."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def emit(self, record):
        try:
            self._callback(record.getMessage())
        except Exception:  # noqa: BLE001 - a logging tap must never raise
            pass


class WatchController:
    """Start/stop the watch loop in a background thread. Tk-free, testable."""

    def __init__(self, runner=watch.run):
        self._runner = runner
        self._stop = threading.Event()
        self._stop_requested = False
        self._thread = None
        self._lock = threading.Lock()
        self._error = None
        self._fires = 0
        self._last_fired = None
        # A per-instance Logger (not via getLogger) so multiple controllers don't
        # share handlers. propagate=False keeps watch logs out of the root logger.
        self._logger = logging.Logger("claude_continue.gui")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(_FireTap(self._on_log))

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

    def _on_log(self, message: str) -> None:
        if message.startswith("fired ->"):
            with self._lock:
                self._fires += 1
                self._last_fired = datetime.now()


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


def watch_explanation(cfg) -> str:
    """Plain-language description of what 'Start watching' will do, given the
    config. Shown in the idle state so the user knows the effect before clicking.
    Pure (no Tk) so it's unit-testable."""
    when = "When you start watching, claude-continue waits for your Claude usage window to reset, then "
    if cfg.exec_cmd:
        return when + ("runs `%s` headlessly — so work resumes the instant your quota refreshes." % cfg.exec_cmd)
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
    from tkinter import font as tkfont, messagebox

    controller = WatchController()
    # Config is snapshotted once at startup; edits to the config file / env take
    # effect on the next launch, not mid-session.
    app_cfg = resolve()
    poll = {"reset_at": None, "note": "", "busy": False,
            "sessions": None, "sessions_note": "", "sessions_busy": False}
    # self-update state machine: idle -> checking -> checked -> [applying -> done] / error
    # `auto` marks a background (startup) check that colours the button without prompting.
    upd = {"phase": "idle", "info": None, "msg": "", "error": None, "auto": False}
    # self-removal state: idle -> removing -> done (quit; the helper deletes the app) / error
    rem = {"phase": "idle", "error": None}

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
                              font="TkFixedFont", justify="left", anchor="w")
    sessions_label.pack(fill="x", padx=16, pady=(0, 10))
    explain = tk.Label(root, text="", fg="#555", wraplength=430, justify="center")
    explain.pack(padx=16, pady=(0, 10))
    button = tk.Button(root, text="▶  Start watching", width=22, height=2)
    button.pack()
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
                else:
                    poll["sessions"] = None
                    poll["sessions_note"] = "macOS/iTerm2 only (or set tmux mode)"
            except Exception as e:  # noqa: BLE001
                poll["sessions"] = None
                poll["sessions_note"] = "%s query failed: %s" % (
                    "tmux" if app_cfg.tmux else "iTerm2", str(e)[:50])
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

    def refresh():
        # the pre-watch explanation is only relevant before you start
        explain.config(text="" if controller.is_watching() or controller.is_stopping() else watch_explanation(app_cfg))
        if controller.error:
            dot.config(text="⚠", fg="#a00")
            status.config(text="Stopped")
            detail.config(text="")
            note.config(text=controller.error)
            button.config(text="▶  Start watching", state="normal")
        elif controller.is_stopping():
            # stop requested; worker is finishing an uninterruptible in-flight fire
            dot.config(text="◐", fg="#c80")
            status.config(text="Stopping…")
            detail.config(text="finishing the current cycle")
            note.config(text="")
            button.config(text="Stopping…", state="disabled")
        elif controller.is_watching():
            dot.config(text="●", fg="#22aa22")
            status.config(text="WATCHING")
            detail.config(text=countdown_text())
            button.config(text="⏹  Stop watching", state="normal")
            if controller.last_fired:
                note.config(text="last fired %s ✓  (%d total)" % (controller.last_fired.strftime("%H:%M"), controller.fires), fg="#2a2")
            else:
                note.config(text="")
        else:
            dot.config(text="○", fg="#999")
            status.config(text="Idle")
            detail.config(text="press Start to watch the quota")
            button.config(text="▶  Start watching", state="normal")
            note.config(text="")
        sessions_label.config(text=format_sessions(
            poll["sessions"], poll["sessions_note"],
            watching=controller.is_watching(), cfg=app_cfg))
        root.after(1000, refresh)

    def toggle():
        if controller.is_watching():
            controller.request_stop()  # non-blocking; UI shows "Stopping…" until the worker exits
        else:
            try:
                from . import action
                action.perform(app_cfg, dry_run=True)  # validate up front; fail clearly
            except ActionError as e:
                note.config(text=str(e), fg="#a00")
                return
            controller.start(app_cfg)
            poll_ccusage()

    button.config(command=toggle)

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
        color = update_button_color(upd["info"], frozen=update.is_frozen())
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
