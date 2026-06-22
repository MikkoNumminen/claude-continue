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


def _cmd_batch_quote(arg: str) -> str:
    """Quote one token so a ``.cmd`` line passes it to the target exe intact.

    ``subprocess.list2cmdline`` quotes for ``CommandLineToArgvW`` (what the exe
    parses) but NOT for cmd.exe's batch layer, which runs first: ``%`` triggers
    variable expansion even inside quotes, and ``& | < > ^ ( )`` are operators in
    any *unquoted* token (list2cmdline leaves a token without spaces unquoted, so a
    config value like ``a&b`` would inject). So we do both layers:

    1. ``CommandLineToArgvW``-correct double-quoting (backslashes before a quote
       doubled, embedded quotes ``\\``-escaped) — and we quote *every* token so
       cmd's operators are always literal inside the quotes;
    2. double every ``%`` to ``%%`` so cmd yields a single literal ``%`` to the exe.
    """
    out = ['"']
    backslashes = 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
            out.append(ch)
        elif ch == '"':
            out.append("\\" * backslashes + '\\"')  # escape the quote + the run of \ before it
            backslashes = 0
        else:
            backslashes = 0
            out.append(ch)
    out.append("\\" * backslashes + '"')  # double the \ that would otherwise escape the closing "
    return "".join(out).replace("%", "%%")


def wrapper_body(inner, *, wsl: bool) -> str:
    """Contents of the wrapper script the task invokes.

    We point schtasks /tr at a single wrapper path (not a full command line),
    which sidesteps schtasks's fragile /tr quoting — the real command, with all
    its spaces/quotes, lives inside the wrapper where we control the quoting.
    """
    if wsl:
        return "#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in inner) + "\n"
    # NOT list2cmdline: it quotes for the exe's argv parser but not for cmd.exe's
    # batch layer (% expansion, & | < > operators) — see _cmd_batch_quote.
    return "@echo off\r\n" + " ".join(_cmd_batch_quote(a) for a in inner) + "\r\n"


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
    # Quote the action so Task Scheduler parses a spaced install path correctly
    # (e.g. %LOCALAPPDATA% under "C:\Users\First Last\..."). The wrapper path is
    # app-controlled; quoting an unspaced path is harmless.
    return '"%s"' % wrapper


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
    # also clean up the wrapper script we wrote at install time
    try:
        wrapper_path().unlink()
    except OSError:
        pass
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
