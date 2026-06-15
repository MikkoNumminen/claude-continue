"""Self-update: check the latest GitHub release and replace the running app.

The repo is public, so the releases API and asset downloads need no auth — plain
stdlib ``urllib``. Auto-replace works for the frozen binaries (PyInstaller .app /
.exe); from source there's nothing to replace, so it points you at git.

Integrity: the GitHub API ships a per-asset SHA-256, which we verify after
download before installing. That defends the download (corruption / on-path
tampering of the bytes vs. what the API listed) — it does NOT defend a fully
compromised release/repo, which would need a detached signature checked against a
pinned key. For a personal tool the trust root is "you trust this GitHub repo".
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__, osenv

REPO = "MikkoNumminen/claude-continue"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases/latest" % REPO

_UA = {"Accept": "application/vnd.github+json", "User-Agent": "claude-continue-updater"}
# GitHub serves release assets from these hosts (the download URL redirects).
_ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}

# System CA bundles to fall back on when the (frozen-app) default context has
# none. A PyInstaller .app bundles its own OpenSSL whose compiled-in OPENSSLDIR
# points at the build machine, so create_default_context() can load ZERO CAs and
# every HTTPS request dies with "unable to get local issuer certificate". These
# absolute paths exist on the user's real OS regardless of the bundle.
_CA_FALLBACKS = (
    "/etc/ssl/cert.pem",                     # macOS + many *nix
    "/private/etc/ssl/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",  # Homebrew (Apple silicon)
    "/usr/local/etc/openssl@3/cert.pem",     # Homebrew (Intel)
    "/etc/pki/tls/certs/ca-bundle.crt",      # RHEL/Fedora
    "/etc/ssl/certs/ca-certificates.crt",    # Debian/Ubuntu
)


def _ca_count(ctx) -> int:
    try:
        return ctx.cert_store_stats().get("x509_ca", 0)
    except Exception:  # noqa: BLE001
        return 0


def _ssl_context():
    """A verifying TLS context that still works inside the frozen .app.

    Windows pulls CAs from the system store and a from-source run finds the
    configured bundle, so the default context is fine there. Only the frozen
    macOS app tends to load zero CAs — when it does, load the OS system bundle.
    """
    ctx = ssl.create_default_context()
    # In a frozen app the bundled OpenSSL may load ZERO CAs, or a stale/partial
    # bundle missing a needed root — so when frozen, additively merge the OS
    # system bundle. load_verify_locations only ADDS trust anchors; it never
    # weakens the (still hostname- and chain-verifying) context.
    if is_frozen() or _ca_count(ctx) == 0:
        for path in _CA_FALLBACKS:
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(cafile=path)
                except (OSError, ssl.SSLError):
                    continue
                break  # one system bundle (e.g. /etc/ssl/cert.pem) is enough
    return ctx


def _open(req, timeout):
    """Default opener for check(): urlopen with the frozen-app-safe TLS context."""
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


class UpdateError(Exception):
    """A self-update step failed."""


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    newer: bool
    asset_name: str | None
    asset_url: str | None
    asset_digest: str | None = None  # "sha256:<hex>"
    error: str | None = None


# --- version comparison -----------------------------------------------------

def _version_tuple(v: str):
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
        a, b = _version_tuple(latest), _version_tuple(current)
        n = max(len(a), len(b))
        a = a + (0,) * (n - len(a))  # pad so 1.0 == 1.0.0
        b = b + (0,) * (n - len(b))
        return a > b
    except Exception:  # noqa: BLE001
        return False


# --- platform asset selection -----------------------------------------------

def _matches_platform(name: str) -> bool:
    """True if `name` is THIS platform's installable build (not a sidecar)."""
    n = name.lower()
    plat = osenv.detect()
    if plat == osenv.MACOS:
        return n.endswith(".zip") and "macos" in n
    if plat in (osenv.WINDOWS, osenv.WSL):
        return n.endswith(".exe")
    return False


def asset_for_platform(assets):
    """assets: iterable of (name, url). Returns (name, url) for this platform, or (None, None)."""
    for name, url in assets:
        if _matches_platform(name):
            return name, url
    return None, None


# --- check ------------------------------------------------------------------

_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}  # worth retrying


def _is_transient(e) -> bool:
    """A retryable network blip (GitHub 5xx/429, timeout, connection reset)."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in _TRANSIENT_HTTP
    # URLError wraps socket errors (timeout/DNS/reset); TimeoutError/ConnectionError too
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))


def check(*, timeout: float = 15.0, opener=_open, current: str | None = None,
          attempts: int = 3, sleep=time.sleep) -> UpdateInfo:
    """Query the latest release and report whether it's newer than us.

    Retries transient failures (GitHub 502/503/504, timeouts) a few times with a
    short backoff so a momentary blip doesn't surface as 'update check failed'.
    Never raises — any final failure is reported via UpdateInfo.error.
    """
    current = current or __version__
    req = urllib.request.Request(API_URL, headers=_UA)
    data = None
    for attempt in range(attempts):
        try:
            with opener(req, timeout=timeout) as resp:
                data = json.load(resp)
            break
        except Exception as e:  # noqa: BLE001 - reported, never raised
            if attempt < attempts - 1 and _is_transient(e):
                sleep(1.0 * (attempt + 1))  # 1s, 2s backoff
                continue
            return UpdateInfo(current, None, False, None, None, error=str(e)[:100])
    latest = data.get("tag_name")
    raw = data.get("assets", [])
    name, url = asset_for_platform((a["name"], a["browser_download_url"]) for a in raw)
    digest = next((a.get("digest") for a in raw if a["name"] == name), None) if name else None
    newer = is_newer(latest, current) if latest else False
    return UpdateInfo(current=current, latest=latest, newer=newer,
                      asset_name=name, asset_url=url, asset_digest=digest)


# --- download + integrity ---------------------------------------------------

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def macos_bundle_path():
    path = os.path.realpath(sys.executable)
    for _ in range(4):
        path = os.path.dirname(path)
        if path.endswith(".app"):
            return path
    return None


def _check_url(url: str) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.netloc not in _ALLOWED_HOSTS:
        raise UpdateError("refusing to download from untrusted URL: %s" % url)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_digest(path: str, digest: str | None) -> None:
    if not digest:
        raise UpdateError("release asset has no checksum to verify against")
    algo, _, expected = digest.partition(":")
    if algo != "sha256" or not expected:
        raise UpdateError("unsupported asset digest: %s" % digest)
    actual = _sha256(path)
    if actual.lower() != expected.lower():
        raise UpdateError("checksum mismatch (expected %s…, got %s…)" % (expected[:12], actual[:12]))


def _download(url: str, dest: str, timeout: float) -> None:
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": _UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


# --- apply ------------------------------------------------------------------

def apply_update(info: UpdateInfo, *, timeout: float = 180.0, relaunch: bool = True,
                 bundle_override: str | None = None) -> str:
    """Download (+ verify checksum) the asset and replace the running app.

    Raises UpdateError on any problem; the old app keeps running.
    """
    if not info.asset_url or not info.asset_name:
        raise UpdateError("no downloadable build for this platform")
    if not is_frozen():
        raise UpdateError("running from source — update with `git pull` (or pip), not the button")

    plat = osenv.detect()
    tmp = tempfile.mkdtemp(prefix="cc-update-")
    # Use a self-chosen filename, never the (attacker-influenceable) asset name,
    # so it can't traverse paths or inject into the Windows helper script.
    dest = os.path.join(tmp, "claude-continue-update" + (".zip" if plat == osenv.MACOS else ".exe"))
    try:
        _download(info.asset_url, dest, timeout)
        _verify_digest(dest, info.asset_digest)
    except UpdateError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError("download failed: %s" % e) from e

    if plat == osenv.MACOS:
        try:
            return _apply_macos(dest, tmp, relaunch, bundle_override)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # bundle is installed; tmp no longer needed
    if plat in (osenv.WINDOWS, osenv.WSL):
        return _apply_windows(dest, relaunch)  # the helper consumes tmp after we exit
    shutil.rmtree(tmp, ignore_errors=True)
    raise UpdateError("auto-update isn't supported on %s" % plat)


def _apply_macos(zip_path: str, tmp: str, relaunch: bool, bundle_override: str | None) -> str:
    bundle = bundle_override or macos_bundle_path()
    if not bundle:
        raise UpdateError("couldn't locate the .app bundle to replace")
    # Honor apply_update's "raises UpdateError on any problem" contract: ditto and
    # rename can raise CalledProcessError/OSError, which the CLI would otherwise
    # surface as a raw traceback (the GUI worker's broad except hid it).
    try:
        subprocess.run(["ditto", "-x", "-k", zip_path, tmp], check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError("failed to extract the update: %s" % e) from e
    new_app = os.path.join(tmp, "claude-continue.app")
    if not os.path.isdir(new_app):
        raise UpdateError("update archive didn't contain claude-continue.app")
    backup = bundle + ".old"
    shutil.rmtree(backup, ignore_errors=True)
    try:
        os.rename(bundle, backup)  # macOS lets us move an in-use bundle aside
    except OSError as e:
        raise UpdateError("couldn't move the current app aside: %s" % e) from e
    try:
        subprocess.run(["ditto", new_app, bundle], check=True)
    except Exception as e:  # noqa: BLE001 - roll back to the old bundle
        shutil.rmtree(bundle, ignore_errors=True)
        os.rename(backup, bundle)
        raise UpdateError("install failed, rolled back: %s" % e) from e
    shutil.rmtree(backup, ignore_errors=True)
    if relaunch:
        _spawn_macos_relauncher(bundle)
    return bundle


def _spawn_macos_relauncher(bundle: str) -> None:
    """Detached helper: wait for THIS process to exit, then open the new bundle.
    Avoids LaunchServices re-activating the old dying instance."""
    script = (
        "#!/bin/sh\n"
        "while kill -0 %d 2>/dev/null; do sleep 0.3; done\n" % os.getpid()
        + "open %s\n" % shlex.quote(bundle)
        + 'rm -f "$0"\n'
    )
    path = os.path.join(tempfile.gettempdir(), "claude-continue-relaunch.sh")
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    subprocess.Popen(["/bin/sh", path], **osenv.detached_popen_kwargs())


def windows_swap_script(new_exe: str, target_exe: str, pid: int, relaunch: bool) -> str:
    """The .cmd that waits for us to exit, overwrites the exe, relaunches, cleans up.
    All paths here are app-controlled (never the GitHub asset name)."""
    lines = [
        "@echo off",
        ":wait",
        'tasklist /FI "PID eq %d" 2>NUL | find "%d" >NUL && (ping -n 2 127.0.0.1 >NUL & goto wait)' % (pid, pid),
        'copy /Y "%s" "%s" >NUL' % (new_exe, target_exe),
    ]
    if relaunch:
        lines.append('start "" "%s"' % target_exe)
    lines.append('del "%s" >NUL 2>&1' % new_exe)
    lines.append('del "%~f0"')
    return "\r\n".join(lines) + "\r\n"


def _apply_windows(new_exe: str, relaunch: bool) -> str:
    target = os.path.realpath(sys.executable)
    script_path = os.path.join(tempfile.gettempdir(), "claude-continue-update.cmd")
    try:
        with open(script_path, "w") as f:
            f.write(windows_swap_script(new_exe, target, os.getpid(), relaunch))
        subprocess.Popen(["cmd", "/c", script_path], **osenv.detached_popen_kwargs())
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError("couldn't launch the Windows update helper: %s" % e) from e
    return target
