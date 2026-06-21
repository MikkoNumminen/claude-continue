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
import zipfile
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
        # Windows ships a one-dir build zipped (like macOS), NOT a single .exe: the
        # one-file exe re-unpacked python311.dll into %TEMP% on every launch, which
        # antivirus (e.g. IPVanish Threat Protection) heuristically blocked.
        return n.endswith(".zip") and "windows" in n
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
    assert data is not None  # the loop either broke with data set or returned above
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

    Raises UpdateError on any problem; the old app keeps running. NOTE the
    platform asymmetry: on macOS the swap happens synchronously here, so a clean
    return means the new bundle is installed. On Windows the running build's files
    (the exe + the loaded ``_internal\\python311.dll`` etc.) are locked and CANNOT
    be swapped in-process, so the swap runs in a detached helper AFTER this process
    exits — a clean return there only means the helper was *spawned*, not that the
    swap succeeded. A pending stamp (see _apply_windows_dir) lets
    cleanup_stale_update detect a silently-failed swap on the next launch.
    """
    if not info.asset_url or not info.asset_name:
        raise UpdateError("no downloadable build for this platform")
    if not is_frozen():
        raise UpdateError("running from source — update with `git pull` (or pip), not the button")

    plat = osenv.detect()
    tmp = tempfile.mkdtemp(prefix="cc-update-")
    # Use a self-chosen filename, never the (attacker-influenceable) asset name,
    # so it can't traverse paths or inject into the Windows helper script. Both
    # platforms now ship a .zip (macOS .app / Windows one-dir folder).
    dest = os.path.join(tmp, "claude-continue-update.zip")
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
        try:
            # On success the detached helper consumes tmp after we exit, so DON'T
            # clean it here; only clean if we raise before spawning anything.
            return _apply_windows_dir(dest, tmp, relaunch, target_version=info.latest)
        except UpdateError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
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


# Characters that would break the swap .cmd if they appeared in a substituted
# path. ``"`` ends the quoted token; ``%`` triggers cmd variable expansion even
# inside quotes; ``& < > | ^`` are cmd operators/escape; CR/LF end a statement.
# Of these only ``%`` is actually legal in a Windows path — the rest can't occur
# in a filename — but we refuse all of them rather than emit a malformed script.
_BAT_UNSAFE_CHARS = set('%&<>|^"') | {"\r", "\n"}


def _assert_swap_safe_path(path: str, label: str) -> None:
    bad = sorted(_BAT_UNSAFE_CHARS & set(path))
    if bad:
        raise UpdateError(
            "%s contains characters that would break the update script (%s): %r"
            % (label, " ".join(repr(c) for c in bad), path)
        )


_WIN_EXE_NAME = "claude-continue.exe"


def _install_dir() -> str:
    """The directory the one-dir Windows build lives in (the exe's parent).

    The Windows release is a PyInstaller *one-dir* build: ``claude-continue.exe``
    sits next to an ``_internal\\`` folder holding ``python311.dll`` and the rest.
    The unit of install/update/removal is therefore this whole directory, not the
    single exe — the one-file build's per-launch %TEMP% unpack of python311.dll is
    what antivirus heuristics (e.g. IPVanish Threat Protection) blocked."""
    return os.path.dirname(os.path.realpath(sys.executable))


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    """Extract ``zf`` into ``dest``, refusing any entry that would escape ``dest``
    (zip-slip). The asset is SHA-256-verified already, but a traversal guard is
    cheap insurance against a crafted archive."""
    dest_abs = os.path.realpath(dest)
    for member in zf.namelist():
        target = os.path.realpath(os.path.join(dest, member))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            raise UpdateError("refusing zip entry that escapes the target: %r" % member)
    zf.extractall(dest)


def windows_dir_swap_script(install_dir: str, new_tree: str, relaunch: bool, *,
                            pid: int, wait_s: int = 30) -> str:
    """The .cmd that waits for us to exit, swaps the whole install DIRECTORY, then
    relaunches and cleans up. All paths here are app-controlled (never the asset
    name). The new build is a *directory* (one-dir), so this swaps a folder — the
    Windows analogue of the macOS bundle swap, with the v0.8.x PID-wait.

    Console-less (CREATE_NO_WINDOW), so the wait POLLS for our ``pid`` to disappear
    via FILE REDIRECTION (anonymous pipes don't connect window-less) with
    ``waitfor`` for each ~1s delay (``timeout``/``ping`` don't block window-less),
    hard-capped by a counter so it can never hang. A failed/absent ``tasklist``
    (checked via errorlevel) is treated as "can't confirm exit" and keeps waiting —
    never a swap-while-alive.

    Only once our process is gone (so the loaded DLLs are unlocked) does it swap:
      * clear any stale ``<dir>.old`` backup,
      * ``move`` the live install dir aside to ``<dir>.old`` (same-volume rename),
      * if that move did NOT free the path, abort without merging — the old install
        stays intact (un-updated beats a half-merged tree),
      * ``robocopy /MOVE`` the new tree into place (cross-volume safe; robocopy
        exit code >= 8 is a real failure),
      * ROLL BACK if robocopy failed or the new exe is missing: drop the partial
        dir and move ``<dir>.old`` home, so the path is never left broken,
      * relaunch only if an exe actually exists, then best-effort ``rmdir`` the
        backup (``cleanup_stale_update`` finishes a locked one on the next launch).

    NOTE (flagged): unit-tested for its text but NOT yet run against a live install
    on real Windows. The move-aside + abort + rollback keep the worst case bounded
    and never-bricked; verify on hardware before relying on it."""
    old = install_dir + ".old"
    exe = install_dir + "\\" + _WIN_EXE_NAME
    wait_file = "%TEMP%\\cc-update-wait.txt"
    lines = [
        "@echo off",
        # Wait for our PID to exit (file-redirection poll; %%_i%% caps the loop).
        "set _i=0",
        ":ccwait",
        'tasklist /FI "PID eq %d" /NH > "%s" 2>NUL' % (pid, wait_file),
        # tasklist failed/absent -> can't confirm exit; keep waiting, never swap.
        "if errorlevel 1 goto cctick",
        'findstr /C:"%d" "%s" >NUL || goto ccswap' % (pid, wait_file),
        ":cctick",
        "set /a _i+=1",
        "if %%_i%% GEQ %d goto ccswap" % wait_s,
        "waitfor /t 1 ClaudeContinuePoll >NUL 2>&1",
        "goto ccwait",
        ":ccswap",
        'del "%s" >NUL 2>&1' % wait_file,
        # clear any stale backup, then move the live install dir aside.
        'rmdir /S /Q "%s" >NUL 2>&1' % old,
        'move /Y "%s" "%s" >NUL 2>&1' % (install_dir, old),
        # move-aside didn't free the path -> bail without merging onto the old tree.
        'if exist "%s" goto ccrelaunch' % exe,
        # copy the new tree in (robocopy is cross-volume safe; /MOVE clears source).
        'robocopy "%s" "%s" /E /MOVE /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >NUL' % (new_tree, install_dir),
        "if errorlevel 8 goto ccrollback",
        'if exist "%s" goto ccrelaunch' % exe,
        ":ccrollback",
        # new tree didn't land -> drop any partial and restore the old install.
        'rmdir /S /Q "%s" >NUL 2>&1' % install_dir,
        'move /Y "%s" "%s" >NUL 2>&1' % (old, install_dir),
        ":ccrelaunch",
    ]
    if relaunch:
        # only relaunch if an exe actually exists at the path (new or rolled-back).
        lines.append('if exist "%s" start "" "%s"' % (exe, exe))
    lines += [
        'rmdir /S /Q "%s" >NUL 2>&1' % old,   # best-effort; cleanup_stale_update gets a locked one
        'del "%~f0"',
    ]
    return "\r\n".join(lines) + "\r\n"


def _allow_foreground_handoff() -> None:
    """Best-effort: let the next process (the relaunched app) take the foreground
    after we exit, so its window isn't stuck behind whatever the user clicked."""
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # type: ignore[attr-defined]  # ASFW_ANY; windll is Windows-only
    except (OSError, AttributeError):
        pass


_PENDING_SUFFIX = ".cc-update-pending"


def _apply_windows_dir(zip_path: str, tmp: str, relaunch: bool, target_version: str | None = None) -> str:
    """Extract the one-dir zip and spawn the detached directory-swap helper.

    Returns the exe path on success (helper spawned). Raises UpdateError on any
    problem BEFORE spawning, leaving the running install untouched."""
    exe = os.path.realpath(sys.executable)
    install = os.path.dirname(exe)
    # Refuse to build a malformed .cmd (which could fail mid-swap) — fail the
    # update cleanly instead, leaving the running app intact.
    _assert_swap_safe_path(install, "the app folder")
    # Extract in-process (stdlib zipfile, no shell-out) so we can validate the tree
    # before touching the install. The release zip wraps a top-level claude-continue/.
    extract_to = os.path.join(tmp, "extracted")
    try:
        os.makedirs(extract_to, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, extract_to)
    except UpdateError:
        raise
    except (OSError, zipfile.BadZipFile) as e:
        raise UpdateError("failed to extract the update: %s" % e) from e
    new_tree = os.path.join(extract_to, "claude-continue")
    if not os.path.isfile(os.path.join(new_tree, _WIN_EXE_NAME)):
        raise UpdateError("update archive didn't contain claude-continue\\%s" % _WIN_EXE_NAME)
    _assert_swap_safe_path(new_tree, "the download path")
    # Pending stamp (Python-managed, no batch), written INSIDE the install dir next
    # to the exe. The swap moves the dir aside, so a successful swap leaves the new
    # dir stamp-less (no warning); a rollback brings it back and cleanup_stale_update
    # warns. The return only means "helper spawned", not "swap succeeded".
    try:
        with open(exe + _PENDING_SUFFIX, "w") as f:
            f.write((target_version or "").strip())
    except OSError:
        pass  # best-effort; the swap still proceeds
    script_path = os.path.join(tempfile.gettempdir(), "claude-continue-update.cmd")
    try:
        # newline="" so the \r\n line endings aren't re-translated to \r\r\n.
        with open(script_path, "w", newline="") as f:
            f.write(windows_dir_swap_script(install, new_tree, relaunch, pid=os.getpid()))
        # CREATE_NO_WINDOW (not DETACHED): a detached cmd's pipes/ping fail, and the
        # script is written to tolerate the no-console environment (see above).
        subprocess.Popen(["cmd", "/c", script_path], **osenv.no_window_kwargs())
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError("couldn't launch the Windows update helper: %s" % e) from e
    if relaunch:
        _allow_foreground_handoff()
    return exe


def cleanup_stale_update() -> str | None:
    """Tidy up after a previous Windows self-update; return a one-line warning if
    the last update silently failed, else None. Best-effort; never raises.

    Three jobs: (1) remove the ``<install>.old`` DIRECTORY the swap left behind
    (locked until old processes exit, so we finish it here on the next launch);
    (2) reap leaked ``cc-update-*`` temp dirs from apply_update; (3) check the
    pending stamp — if it survived and we're still on the old version, the swap
    didn't land."""
    if not (is_frozen() and osenv.detect() in (osenv.WINDOWS, osenv.WSL)):
        return None
    warning = None
    exe = os.path.realpath(sys.executable)
    install = os.path.dirname(exe)
    try:
        old = install + ".old"
        # Only reap the leftover when the live exe is present. If the exe is
        # missing (a swap rolled back but hasn't been renamed home, or a
        # half-finished move), the .old dir may be the ONLY surviving copy — never
        # delete it then.
        if os.path.isfile(exe) and os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass  # still locked, or vanished — next launch tries again
    # pending stamp: a surviving stamp whose target version is newer than the one
    # we're now running means the swap silently failed (Windows returns before it).
    try:
        pending = exe + _PENDING_SUFFIX
        if os.path.exists(pending):
            with open(pending) as f:
                want = f.read().strip()
            os.remove(pending)  # clear it so we warn at most once
            if want and is_newer(want, __version__):
                warning = "the last update to %s didn't complete — try Update again" % want
    except OSError:
        pass
    # reap leaked temp dirs from apply_update's mkdtemp(prefix="cc-update-")
    try:
        import glob
        for d in glob.glob(os.path.join(tempfile.gettempdir(), "cc-update-*")):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    return warning
