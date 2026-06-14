"""Platform detection and the few OS-specific primitives the rest of the code needs.

Supported targets: macOS, native Windows, and WSL (Linux under Windows). Plain
Linux is recognised but only the headless (``--exec``) action works there.

``detect()`` honours the ``CLAUDE_CONTINUE_PLATFORM`` env var so tests (and the
odd power user) can force a platform without being on it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

MACOS = "macos"
WINDOWS = "windows"
WSL = "wsl"
LINUX = "linux"

PLATFORM_ENV = "CLAUDE_CONTINUE_PLATFORM"


def _proc_version() -> str:
    try:
        with open("/proc/version") as f:
            return f.read()
    except OSError:
        return ""


def detect() -> str:
    override = os.environ.get(PLATFORM_ENV)
    if override:
        return override
    if sys.platform == "darwin":
        return MACOS
    if os.name == "nt" or sys.platform.startswith("win"):
        return WINDOWS
    if "microsoft" in _proc_version().lower() or os.environ.get("WSL_DISTRO_NAME"):
        return WSL
    return LINUX


def is_macos() -> bool:
    return detect() == MACOS


def is_windows() -> bool:
    return detect() == WINDOWS


def is_wsl() -> bool:
    return detect() == WSL


def uses_launchd() -> bool:
    return detect() == MACOS


def uses_task_scheduler() -> bool:
    # native Windows runs schtasks directly; WSL drives schtasks.exe via interop
    return detect() in (WINDOWS, WSL)


def resolve_argv(argv: list) -> list:
    """Resolve argv[0] on PATH and make it runnable.

    On Windows, ``node``/``npx``/``claude`` are usually ``.cmd`` shims that
    ``CreateProcess`` (and thus subprocess without a shell) cannot launch
    directly — wrap those via ``cmd /c``.
    """
    if not argv:
        return argv
    exe = shutil.which(argv[0]) or argv[0]
    rest = list(argv[1:])
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe] + rest
    return [exe] + rest


def detached_popen_kwargs() -> dict:
    """Kwargs so a spawned child outlives us and isn't tied to our console."""
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists. Cross-platform.

    On POSIX, signal 0 probes existence. On Windows, ``os.kill(pid, 0)`` isn't
    reliable, so query the process handle via the Win32 API.
    """
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # exists but couldn't read exit code
        finally:
            kernel32.CloseHandle(handle)
    import errno

    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        if e.errno == errno.EPERM:
            return True
        return False
    return True
