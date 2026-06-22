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


def _argv_quote(arg: str) -> str:
    """Quote one token per the ``CommandLineToArgvW`` / MSVC-runtime rules the target
    exe parses: wrap in double quotes, escape an embedded ``"`` as ``\\"``, and double
    any run of backslashes that immediately precedes a ``"`` (incl. the closing one)."""
    out = ['"']
    backslashes = 0
    for ch in arg:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            out.append("\\" * (backslashes * 2 + 1))  # double the run, +1 to escape the "
            out.append('"')
        else:
            if backslashes:
                out.append("\\" * backslashes)
            out.append(ch)
        backslashes = 0
    out.append("\\" * (backslashes * 2))  # backslashes before the closing " must be doubled
    out.append('"')
    return "".join(out)


# cmd.exe metacharacters that a leading ^ makes literal. '%' is NOT here — caret does
# not make it literal in a batch; it's doubled to %% instead.
_CMD_META = set('()!^"<>&|')


def _cmd_arg(arg: str) -> str:
    """Embed an *argument* in a console-less ``.cmd`` so BOTH parsers that run in series
    deliver the exact original token. cmd.exe's batch layer runs first; the exe's
    ``CommandLineToArgvW`` second. We argv-quote for the exe, then CARET-escape every
    cmd metacharacter — INCLUDING the quotes — so cmd's quote-state never engages and no
    operator (``& | < >``) can be live (the trap a plain ``\\"`` falls into: it flips
    cmd's quote-state and re-exposes operators). ``%`` is doubled so the exe gets a
    literal percent. After cmd strips the carets, the exe re-parses the argv-quoted form."""
    out = []
    for ch in _argv_quote(arg):
        if ch in _CMD_META:
            out.append("^")
        out.append(ch)
    return "".join(out).replace("%", "%%")


def _cmd_command(path: str) -> str:
    """The command (argv[0]) needs REAL quotes so cmd can resolve a spaced path — its
    quotes MUST engage cmd's quote-state (caret-escaping them would hide the command from
    cmd). A program path can't contain ``" < > |``; ``& ^ ( )`` are literal inside the
    real quotes; ``%`` is doubled."""
    return '"' + path.replace("%", "%%") + '"'


def wrapper_body(inner, *, wsl: bool) -> str:
    """Contents of the wrapper script the task invokes.

    We point schtasks /tr at a single wrapper path (not a full command line),
    which sidesteps schtasks's fragile /tr quoting — the real command, with all
    its spaces/quotes, lives inside the wrapper where we control the quoting.
    """
    if wsl:
        return "#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in inner) + "\n"
    # NOT list2cmdline: it quotes for the exe's argv parser but not for cmd.exe's batch
    # layer (% expansion, & | < > operators, quote-state flips). argv[0] is the command
    # (real-quoted so cmd resolves a spaced path); the rest are caret-escaped args.
    parts = [_cmd_command(inner[0])] + [_cmd_arg(a) for a in inner[1:]]
    return "@echo off\r\n" + " ".join(parts) + "\r\n"


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
