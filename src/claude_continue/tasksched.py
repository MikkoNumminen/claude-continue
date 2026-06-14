"""Windows Task Scheduler backend (the launchd equivalent on Windows/WSL).

Registers ``claude-continue watch`` to run at logon via ``schtasks``. Under WSL
the task lives on the Windows side and drives the loop back into the distro with
``wsl.exe -d <distro> -e claude-continue watch`` (schtasks itself is reached
through WSL interop).
"""

from __future__ import annotations

import os
import shutil
import subprocess

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


def build_run_command(launch_argv, watch_flags, cfg) -> str:
    """The /tr command string schtasks runs at logon (Windows-quoted)."""
    inner = list(launch_argv) + ["watch"] + list(watch_flags)
    if osenv.is_wsl():
        distro = os.environ.get("WSL_DISTRO_NAME", "")
        argv = ["wsl.exe"] + (["-d", distro] if distro else []) + ["-e"] + inner
    else:
        argv = inner
    return subprocess.list2cmdline(argv)


def install(launch_argv, watch_flags, cfg) -> str:
    tr = build_run_command(launch_argv, watch_flags, cfg)
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
