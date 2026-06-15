"""Platform detection and the few OS-specific primitives the rest of the code needs.

Supported targets: macOS, native Windows, and WSL (Linux under Windows). Plain
Linux is recognised but only the headless (``--exec``) action works there.

``detect()`` honours the ``CLAUDE_CONTINUE_PLATFORM`` env var so tests (and the
odd power user) can force a platform without being on it.
"""

from __future__ import annotations

import functools
import os
import shlex
import shutil
import subprocess
import sys

MACOS = "macos"
WINDOWS = "windows"
WSL = "wsl"
LINUX = "linux"

PLATFORM_ENV = "CLAUDE_CONTINUE_PLATFORM"


@functools.lru_cache(maxsize=1)
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


def split_command(cmd: str) -> list:
    """Split a command string into argv, correctly per platform.

    POSIX ``shlex`` treats ``\\`` as an escape, which silently eats Windows path
    separators (``C:\\tools\\x.exe`` -> ``C:toolsx.exe``). On Windows we split
    non-POSIX (backslashes preserved) and then strip the surrounding quotes
    shlex leaves on quoted tokens.
    """
    if os.name == "nt":
        out = []
        for tok in shlex.split(cmd, posix=False):
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
                tok = tok[1:-1]
            out.append(tok)
        return out
    return shlex.split(cmd)


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
        # `call` lets cmd handle a quoted batch path with spaces (e.g. node under
        # "C:\Program Files\nodejs\npx.cmd") without the bare-`cmd /c` quote-strip bug.
        return ["cmd", "/c", "call", exe] + rest
    return [exe] + rest


def detached_popen_kwargs() -> dict:
    """Kwargs so a spawned child outlives us and isn't tied to our console."""
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def no_window_kwargs() -> dict:
    """subprocess kwargs that suppress the console window a child console program
    (powershell, npx) would otherwise FLASH on Windows when spawned from a
    windowed GUI process that has no console of its own. The GUI polls ccusage and
    the window list on a timer, so without this a console box pops up — and steals
    focus — every few seconds, swallowing the user's keystrokes. No-op off Windows."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists. Cross-platform.

    On POSIX, signal 0 probes existence. On Windows, ``os.kill(pid, 0)`` isn't
    reliable, so query the process handle via the Win32 API.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259
        WAIT_OBJECT_0 = 0x0
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # windll is Windows-only
        # Declare types so 64-bit HANDLEs aren't truncated to int.
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # exists but couldn't read exit code
            if code.value != STILL_ACTIVE:
                return False
            # 259 is ambiguous (it's also a real exit code): confirm via the wait
            # state — a signaled process object has genuinely exited.
            return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
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
