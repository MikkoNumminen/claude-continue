"""Single-instance pidfile lock for the ``watch`` daemon.

Prevents a manually-started ``watch`` and the launchd-managed one from both
broadcasting. Stale pidfiles (holder process gone) are reclaimed automatically.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

DEFAULT_PIDFILE = Path.home() / ".local" / "state" / "claude-continue" / "watch.pid"


class AlreadyRunning(Exception):
    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"another claude-continue watch is already running (pid {pid})")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:  # no such process
            return False
        if e.errno == errno.EPERM:  # exists but owned by someone else
            return True
        return False
    return True


class PidLock:
    def __init__(self, path: Path = DEFAULT_PIDFILE):
        self.path = Path(path)
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing = int(self.path.read_text().strip())
            except (ValueError, OSError):
                existing = None
            if existing is not None and existing != os.getpid() and _alive(existing):
                raise AlreadyRunning(existing)
            # else: stale or our own pid — fall through and reclaim
        self.path.write_text(str(os.getpid()))
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self.path.exists() and self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass
        self._acquired = False

    def __enter__(self) -> "PidLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False
