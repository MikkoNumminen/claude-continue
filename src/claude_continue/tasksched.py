"""Windows Task Scheduler backend (the launchd equivalent on Windows/WSL).

Registers ``claude-continue watch`` to run at logon via ``schtasks``. Under WSL
the task lives on the Windows side and drives the loop back into the distro with
``wsl.exe -d <distro> -e claude-continue watch`` (schtasks itself is reached
through WSL interop).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from . import osenv

TASK_NAME = "claude-continue"


def _schtasks() -> str:
    return shutil.which("schtasks") or shutil.which("schtasks.exe") or "schtasks.exe"


def _run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out after %ss" % timeout)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "", "schtasks not found: %s" % e)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _inner_argv(launch_argv, watch_flags) -> list:
    return list(launch_argv) + ["watch"] + list(watch_flags)


def wrapper_body(inner, *, wsl: bool) -> str:
    """Contents of the wrapper script the task invokes.

    We point schtasks /tr at a single wrapper path (not a full command line),
    which sidesteps schtasks's fragile /tr quoting — the real command, with all
    its spaces/quotes, lives inside the wrapper where we control the quoting.
    """
    if wsl:
        return "#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in inner) + "\n"
    return "@echo off\r\n" + subprocess.list2cmdline(inner) + "\r\n"


def wrapper_path() -> Path:
    if osenv.is_wsl():
        return Path.home() / ".config" / "claude-continue" / "run.sh"
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "claude-continue" / "run.cmd"


def tr_value(wrapper: str, *, wsl: bool, distro: str) -> str:
    """The /tr value: a single wrapper path on Windows, or a wsl.exe invocation
    of the wrapper (no embedded spaces beyond the well-behaved path) on WSL."""
    if wsl:
        argv = ["wsl.exe"] + (["-d", distro] if distro else []) + ["-e", "/bin/sh", wrapper]
        return subprocess.list2cmdline(argv)
    return wrapper


def _write_wrapper(launch_argv, watch_flags) -> str:
    inner = _inner_argv(launch_argv, watch_flags)
    path = wrapper_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(wrapper_body(inner, wsl=osenv.is_wsl()))
    if osenv.is_wsl():
        os.chmod(path, 0o755)
    return str(path)


def install(launch_argv, watch_flags, cfg) -> str:
    wrapper = _write_wrapper(launch_argv, watch_flags)
    tr = tr_value(wrapper, wsl=osenv.is_wsl(), distro=os.environ.get("WSL_DISTRO_NAME", ""))
    proc = _run([
        _schtasks(), "/create", "/tn", TASK_NAME, "/tr", tr,
        "/sc", "onlogon", "/rl", "highest", "/f",
    ])
    if proc.returncode != 0:
        raise RuntimeError(
            "schtasks /create failed (%d): %s" % (proc.returncode, (proc.stderr or proc.stdout or "").strip())
        )
    return tr


def uninstall(purge=False) -> bool:
    proc = _run([_schtasks(), "/delete", "/tn", TASK_NAME, "/f"])
    return proc.returncode == 0


def describe():
    """Return (state_word, detail) where state_word ∈ {absent, running, installed}."""
    proc = _run([_schtasks(), "/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"])
    if proc.returncode != 0:
        return ("absent", "no scheduled task registered")
    for line in (proc.stdout or "").splitlines():
        if line.strip().lower().startswith("status:"):
            st = line.split(":", 1)[1].strip()
            word = "running" if st.lower() == "running" else "installed"
            return (word, "Task Scheduler: %s" % st)
    return ("installed", "scheduled task registered")
