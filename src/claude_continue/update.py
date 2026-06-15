"""Self-update: check the latest GitHub release and replace the running app.

The repo is public, so the releases API and asset downloads need no auth — plain
stdlib ``urllib``. Auto-replace works for the frozen binaries (PyInstaller .app /
.exe); when run from source there's nothing to replace, so it points you at git.

The pure parts (version compare, asset selection, ``check``) are unit-tested with
an injected opener; the platform install paths do real filesystem/process work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass

from . import __version__, osenv

REPO = "MikkoNumminen/claude-continue"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases/latest" % REPO

_UA = {"Accept": "application/vnd.github+json", "User-Agent": "claude-continue-updater"}


class UpdateError(Exception):
    """A self-update step failed."""


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    newer: bool
    asset_name: str | None
    asset_url: str | None
    error: str | None = None


# --- version comparison -----------------------------------------------------

def _version_tuple(v: str):
    """Lenient numeric version tuple: 'v0.3.0' -> (0, 3, 0). Non-numeric tails
    (e.g. a '-rc1' suffix) contribute 0 so they never sort as newer."""
    parts = []
    for chunk in v.strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:  # noqa: BLE001
        return False


# --- platform asset selection -----------------------------------------------

def _asset_token() -> str | None:
    plat = osenv.detect()
    if plat == osenv.MACOS:
        return "macos"  # claude-continue-macos-arm64.zip
    if plat in (osenv.WINDOWS, osenv.WSL):
        return ".exe"   # claude-continue-windows-x64.exe
    return None


def asset_for_platform(assets):
    """assets: iterable of (name, url). Returns (name, url) for this platform, or (None, None)."""
    token = _asset_token()
    if token:
        for name, url in assets:
            if token in name.lower():
                return name, url
    return None, None


# --- check ------------------------------------------------------------------

def check(*, timeout: float = 15.0, opener=urllib.request.urlopen, current: str | None = None) -> UpdateInfo:
    """Query the latest release and report whether it's newer than us."""
    current = current or __version__
    try:
        req = urllib.request.Request(API_URL, headers=_UA)
        with opener(req, timeout=timeout) as resp:
            data = json.load(resp)
        latest = data.get("tag_name")
        assets = [(a["name"], a["browser_download_url"]) for a in data.get("assets", [])]
    except Exception as e:  # noqa: BLE001 - any failure -> reported, never raised
        return UpdateInfo(__version__ if current is None else current, None, False, None, None, error=str(e)[:100])
    name, url = asset_for_platform(assets)
    newer = is_newer(latest, current) if latest else False
    return UpdateInfo(current=current, latest=latest, newer=newer, asset_name=name, asset_url=url)


# --- apply ------------------------------------------------------------------

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def macos_bundle_path():
    """The .app bundle the running frozen binary lives in, or None."""
    path = os.path.realpath(sys.executable)
    for _ in range(4):
        path = os.path.dirname(path)
        if path.endswith(".app"):
            return path
    return None


def _download(url: str, dest: str, timeout: float) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def windows_swap_script(new_exe: str, target_exe: str, pid: int, relaunch: bool) -> str:
    """The .cmd that waits for us to exit, overwrites the exe, relaunches, self-deletes.
    A running .exe can't overwrite itself, so a detached helper does it."""
    lines = [
        "@echo off",
        ":wait",
        'tasklist /FI "PID eq %d" 2>NUL | find "%d" >NUL && (ping -n 2 127.0.0.1 >NUL & goto wait)' % (pid, pid),
        'copy /Y "%s" "%s" >NUL' % (new_exe, target_exe),
    ]
    if relaunch:
        lines.append('start "" "%s"' % target_exe)
    lines.append('del "%~f0"')
    return "\r\n".join(lines) + "\r\n"


def apply_update(info: UpdateInfo, *, timeout: float = 180.0, relaunch: bool = True,
                 bundle_override: str | None = None) -> str:
    """Download the asset and replace the running app. Returns the installed path.

    Raises UpdateError on any problem (caller shows it; the old app keeps running).
    """
    if not info.asset_url or not info.asset_name:
        raise UpdateError("no downloadable build for this platform")
    if not is_frozen():
        raise UpdateError("running from source — update with `git pull` (or pip), not the button")

    plat = osenv.detect()
    tmp = tempfile.mkdtemp(prefix="cc-update-")
    asset = os.path.join(tmp, info.asset_name)
    try:
        _download(info.asset_url, asset, timeout)
    except Exception as e:  # noqa: BLE001
        raise UpdateError("download failed: %s" % e) from e

    if plat == osenv.MACOS:
        return _apply_macos(asset, tmp, relaunch, bundle_override)
    if plat in (osenv.WINDOWS, osenv.WSL):
        return _apply_windows(asset, relaunch)
    raise UpdateError("auto-update isn't supported on %s" % plat)


def _apply_macos(zip_path: str, tmp: str, relaunch: bool, bundle_override: str | None) -> str:
    bundle = bundle_override or macos_bundle_path()
    if not bundle:
        raise UpdateError("couldn't locate the .app bundle to replace")
    subprocess.run(["ditto", "-x", "-k", zip_path, tmp], check=True)
    new_app = os.path.join(tmp, "claude-continue.app")
    if not os.path.isdir(new_app):
        raise UpdateError("update archive didn't contain claude-continue.app")
    backup = bundle + ".old"
    shutil.rmtree(backup, ignore_errors=True)
    os.rename(bundle, backup)  # macOS lets us move an in-use bundle aside
    try:
        subprocess.run(["ditto", new_app, bundle], check=True)
    except Exception as e:  # noqa: BLE001 - roll back to the old bundle
        shutil.rmtree(bundle, ignore_errors=True)
        os.rename(backup, bundle)
        raise UpdateError("install failed, rolled back: %s" % e) from e
    shutil.rmtree(backup, ignore_errors=True)
    if relaunch:
        subprocess.Popen(["open", bundle])
    return bundle


def _apply_windows(new_exe: str, relaunch: bool) -> str:
    target = os.path.realpath(sys.executable)
    script_path = os.path.join(tempfile.gettempdir(), "claude-continue-update.cmd")
    with open(script_path, "w") as f:
        f.write(windows_swap_script(new_exe, target, os.getpid(), relaunch))
    subprocess.Popen(["cmd", "/c", script_path], **osenv.detached_popen_kwargs())
    return target
