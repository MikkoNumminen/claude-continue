"""Install/uninstall the ``watch`` loop as a per-user launchd LaunchAgent.

Why a long-lived KeepAlive agent rather than a ``StartCalendarInterval`` job:
the reset time *drifts* every window, so a fixed calendar can't express it. A
persistent ``watch`` process reads ccusage, sleeps to the next reset, fires, and
re-arms — and KeepAlive restarts it if it crashes (but not after a clean
uninstall).

The plist template lives in ``PLIST_TEMPLATE`` (single source of truth; the
``templates/`` copy is documentation and is checked for drift by the tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from string import Template

LABEL = "com.mikko.claude-continue"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "claude-continue.log"
ERR_LOG_PATH = Path.home() / "Library" / "Logs" / "claude-continue.err.log"

PLIST_TEMPLATE = Template(
    """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
$program_args
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key>
    <true/>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$path</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$stdout</string>
  <key>StandardErrorPath</key>
  <string>$stderr</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
"""
)


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def node_path_value(extra: str | None = None) -> str:
    """Build a PATH that includes node's bin dir (nvm node is NOT on launchd's
    default PATH, so ``npx ccusage`` would fail silently without this)."""
    node = shutil.which("node") or shutil.which("npx")
    node_dir = os.path.dirname(node) if node else ""
    parts = [extra, node_dir, "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin"]
    seen = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return ":".join(seen)


def render_plist(program_args, *, label=LABEL, path_value=None, stdout=None, stderr=None) -> str:
    args_xml = "\n".join(
        "    <string>%s</string>" % _xml_escape(str(a)) for a in program_args
    )
    return PLIST_TEMPLATE.substitute(
        label=label,
        program_args=args_xml,
        path=_xml_escape(path_value if path_value is not None else node_path_value()),
        stdout=_xml_escape(str(stdout or LOG_PATH)),
        stderr=_xml_escape(str(stderr or ERR_LOG_PATH)),
    )


def _domain() -> str:
    return "gui/%d" % os.getuid()


def _service() -> str:
    return "%s/%s" % (_domain(), LABEL)


def _run(cmd, check=False):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            "command failed (%d): %s\n%s" % (proc.returncode, " ".join(cmd), proc.stderr.strip())
        )
    return proc


def install(program_args, *, path_value=None, stdout=None, stderr=None) -> str:
    """Write the plist and (re)load the agent. Returns the plist path."""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(
        render_plist(program_args, path_value=path_value, stdout=stdout, stderr=stderr)
    )

    # Clear any prior instance, then bootstrap. Fall back to legacy load on older macOS.
    _run(["launchctl", "bootout", _service()])  # ignore errors (may not be loaded)
    boot = _run(["launchctl", "bootstrap", _domain(), str(PLIST_PATH)])
    if boot.returncode != 0:
        legacy = _run(["launchctl", "load", "-w", str(PLIST_PATH)])
        if legacy.returncode != 0:
            raise RuntimeError(
                "failed to load agent:\nbootstrap: %s\nload: %s"
                % (boot.stderr.strip(), legacy.stderr.strip())
            )
    else:
        _run(["launchctl", "enable", _service()])
        _run(["launchctl", "kickstart", "-k", _service()])
    return str(PLIST_PATH)


def uninstall(*, purge=False) -> bool:
    """Stop the agent (and optionally delete the plist). Returns True if a plist existed."""
    out = _run(["launchctl", "bootout", _service()])
    if out.returncode != 0 and PLIST_PATH.exists():
        _run(["launchctl", "unload", "-w", str(PLIST_PATH)])
    existed = PLIST_PATH.exists()
    if purge and existed:
        PLIST_PATH.unlink()
    return existed


def status() -> str:
    proc = _run(["launchctl", "print", _service()])
    if proc.returncode != 0:
        return "not loaded (%s)" % proc.stderr.strip()
    return proc.stdout
