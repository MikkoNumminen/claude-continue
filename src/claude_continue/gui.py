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
from datetime import datetime, timezone

from . import ccusage, watch
from .action import ActionError
from .config import resolve
from .lock import AlreadyRunning


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
            self._error = None
            thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self._thread = thread
            thread.start()

    def stop(self, timeout=5.0) -> None:
        self._stop.set()
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


def run() -> None:  # pragma: no cover - exercised manually; logic lives in WatchController
    """Open the toggle window. Imports tkinter lazily so the rest of the package
    doesn't require a display."""
    import tkinter as tk
    from tkinter import font as tkfont

    controller = WatchController()
    poll = {"reset_at": None, "note": "", "busy": False}

    root = tk.Tk()
    root.title("claude-continue")
    root.geometry("380x220")
    root.resizable(False, False)

    dot = tk.Label(root, text="○", font=tkfont.Font(size=30))
    dot.pack(pady=(20, 0))
    status = tk.Label(root, text="Idle", font=tkfont.Font(size=15, weight="bold"))
    status.pack()
    detail = tk.Label(root, text="press Start to watch the quota", fg="#666")
    detail.pack(pady=(2, 14))
    button = tk.Button(root, text="▶  Start watching", width=22, height=2)
    button.pack()
    note = tk.Label(root, text="", fg="#a00", wraplength=340)
    note.pack(pady=(10, 0))

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
        if controller.error:
            dot.config(text="⚠", fg="#a00")
            status.config(text="Stopped")
            detail.config(text="")
            note.config(text=controller.error)
            button.config(text="▶  Start watching")
        elif controller.is_watching():
            dot.config(text="●", fg="#22aa22")
            status.config(text="WATCHING")
            detail.config(text=countdown_text())
            button.config(text="⏹  Stop watching")
            if controller.last_fired:
                note.config(text="last fired %s ✓  (%d total)" % (controller.last_fired.strftime("%H:%M"), controller.fires), fg="#2a2")
            else:
                note.config(text="")
        else:
            dot.config(text="○", fg="#999")
            status.config(text="Idle")
            detail.config(text="press Start to watch the quota")
            button.config(text="▶  Start watching")
            note.config(text="")
        root.after(1000, refresh)

    def toggle():
        if controller.is_watching():
            controller.stop()
        else:
            cfg = resolve()
            try:
                from . import action
                action.perform(cfg, dry_run=True)  # validate up front; fail clearly
            except ActionError as e:
                note.config(text=str(e), fg="#a00")
                return
            controller.start(cfg)
            poll_ccusage()

    button.config(command=toggle)

    def poll_loop():
        if controller.is_watching():
            poll_ccusage()
        root.after(30000, poll_loop)

    def on_close():
        controller.stop(timeout=3)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    root.after(30000, poll_loop)
    root.mainloop()
