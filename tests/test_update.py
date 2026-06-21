import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest import mock

import _support  # noqa: F401

from claude_continue import osenv, update


class _ForcePlatform:
    def __init__(self, plat):
        self.plat = plat

    def __enter__(self):
        self._old = os.environ.get(osenv.PLATFORM_ENV)
        os.environ[osenv.PLATFORM_ENV] = self.plat
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop(osenv.PLATFORM_ENV, None)
        else:
            os.environ[osenv.PLATFORM_ENV] = self._old


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(payload):
    def op(req, timeout=None):
        return _FakeResp(payload)
    return op


def _raising_opener(exc):
    def op(req, timeout=None):
        raise exc
    return op


_ASSETS = [
    ("claude-continue-macos-arm64.zip", "https://x/macos.zip"),
    ("claude-continue-windows-x64.zip", "https://x/win.zip"),
]


class TestVersionCompare(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(update.is_newer("v0.4.0", "0.3.0"))
        self.assertTrue(update.is_newer("v1.0.0", "0.9.9"))

    def test_not_newer(self):
        self.assertFalse(update.is_newer("v0.3.0", "0.3.0"))
        self.assertFalse(update.is_newer("v0.2.0", "0.3.0"))

    def test_prerelease_tail_ignored(self):
        self.assertFalse(update.is_newer("v0.0.0-citest", "0.3.0"))

    def test_equal_despite_different_length(self):
        # 1.0 and 1.0.0 are the same version, not an upgrade either way
        self.assertFalse(update.is_newer("1.0", "1.0.0"))
        self.assertFalse(update.is_newer("1.0.0", "1.0"))

    def test_longer_only_newer_when_greater(self):
        self.assertTrue(update.is_newer("1.0.1", "1.0"))
        self.assertFalse(update.is_newer("1.0", "1.0.1"))


class TestAssetSelection(unittest.TestCase):
    def test_macos(self):
        with _ForcePlatform("macos"):
            self.assertEqual(update.asset_for_platform(_ASSETS), ("claude-continue-macos-arm64.zip", "https://x/macos.zip"))

    def test_windows(self):
        with _ForcePlatform("windows"):
            self.assertEqual(update.asset_for_platform(_ASSETS), ("claude-continue-windows-x64.zip", "https://x/win.zip"))

    def test_linux_has_no_asset(self):
        with _ForcePlatform("linux"):
            self.assertEqual(update.asset_for_platform(_ASSETS), (None, None))

    def test_macos_skips_sidecars(self):
        # .zip.sha256 / .zip.blockmap must not be mistaken for the build
        assets = [
            ("claude-continue-macos-arm64.zip.sha256", "u1"),
            ("claude-continue-macos-arm64.zip.blockmap", "u2"),
            ("claude-continue-macos-arm64.zip", "u3"),
        ]
        with _ForcePlatform("macos"):
            self.assertEqual(update.asset_for_platform(assets), ("claude-continue-macos-arm64.zip", "u3"))

    def test_windows_skips_sha_sidecar(self):
        assets = [
            ("claude-continue-windows-x64.zip.sha256", "u1"),
            ("claude-continue-windows-x64.zip", "u2"),
        ]
        with _ForcePlatform("windows"):
            self.assertEqual(update.asset_for_platform(assets), ("claude-continue-windows-x64.zip", "u2"))

    def test_windows_skips_macos_zip(self):
        # both assets are .zip now; the windows build must match on the "windows" tag,
        # never grab the macOS zip.
        with _ForcePlatform("windows"):
            self.assertEqual(update.asset_for_platform(_ASSETS),
                             ("claude-continue-windows-x64.zip", "https://x/win.zip"))


class TestCheck(unittest.TestCase):
    def test_newer_available(self):
        with _ForcePlatform("macos"):
            info = update.check(opener=_opener({"tag_name": "v0.9.0", "assets": [
                {"name": n, "browser_download_url": u} for n, u in _ASSETS]}), current="0.3.0")
        self.assertTrue(info.newer)
        self.assertEqual(info.latest, "v0.9.0")
        self.assertEqual(info.asset_name, "claude-continue-macos-arm64.zip")
        self.assertIsNone(info.error)

    def test_up_to_date(self):
        with _ForcePlatform("macos"):
            info = update.check(opener=_opener({"tag_name": "v0.3.0", "assets": []}), current="0.3.0")
        self.assertFalse(info.newer)

    def test_error_is_reported_not_raised(self):
        info = update.check(opener=_raising_opener(OSError("network down")), current="0.3.0")
        self.assertIsNotNone(info.error)
        self.assertFalse(info.newer)
        self.assertIsNone(info.latest)


class _FlakyOpener:
    """Fails the first `fail_times` calls with `exc`, then serves `payload`."""
    def __init__(self, fail_times, payload, exc):
        self.fail_times = fail_times
        self.payload = payload
        self.exc = exc
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return _FakeResp(self.payload)


class TestCheckRetry(unittest.TestCase):
    def _http504(self):
        import urllib.error
        return urllib.error.HTTPError("http://x", 504, "Gateway Timeout", {}, None)

    def test_transient_then_success(self):
        op = _FlakyOpener(2, {"tag_name": "v9.9.9", "assets": []}, self._http504())
        with _ForcePlatform("macos"):
            info = update.check(opener=op, current="0.0.0", sleep=lambda *_: None)
        self.assertEqual(op.calls, 3)            # 2 × 504, then success
        self.assertEqual(info.latest, "v9.9.9")
        self.assertIsNone(info.error)

    def test_transient_exhausts_then_reports(self):
        op = _FlakyOpener(99, {}, self._http504())
        info = update.check(opener=op, current="0.0.0", attempts=2, sleep=lambda *_: None)
        self.assertEqual(op.calls, 2)            # bounded by attempts
        self.assertIsNotNone(info.error)

    def test_non_transient_not_retried(self):
        op = _FlakyOpener(99, {}, ValueError("malformed"))
        info = update.check(opener=op, current="0.0.0", attempts=3, sleep=lambda *_: None)
        self.assertEqual(op.calls, 1)            # ValueError isn't transient -> no retry
        self.assertIsNotNone(info.error)


class TestWindowsDirSwapScript(unittest.TestCase):
    def test_moves_dir_then_robocopies_relaunch_selfdelete(self):
        s = update.windows_dir_swap_script(r"C:\app", r"C:\tmp\extracted\claude-continue", relaunch=True, pid=4321)
        # the running build's DLLs are locked, so move the whole dir aside, then
        # robocopy the new tree in (cross-volume safe).
        self.assertIn(r'move /Y "C:\app" "C:\app.old"', s)
        self.assertIn(r'robocopy "C:\tmp\extracted\claude-continue" "C:\app"', s)
        self.assertIn(r'start "" "C:\app\claude-continue.exe"', s)   # relaunch the exe inside the dir
        self.assertIn('del "%~f0"', s)                               # self-delete

    def test_polls_for_pid_capped_with_waitfor_only(self):
        # waits for OUR pid to exit (file-redirection poll, not a pipe), capped by a
        # counter so it can't hang. waitfor (not timeout/ping, which don't delay
        # window-less) supplies each per-iteration delay.
        s = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=True, pid=4321)
        self.assertIn('tasklist /FI "PID eq 4321"', s)
        self.assertIn('findstr /C:"4321"', s)
        self.assertIn("goto ccwait", s)                       # the poll loop
        self.assertIn("waitfor /t 1 ", s)                     # per-iteration delay
        # a failed/absent tasklist must NOT trigger an immediate swap-while-alive:
        # it's gated on errorlevel and keeps waiting (bounded by the cap).
        self.assertIn("if errorlevel 1 goto cctick", s)
        # timeout/ping don't delay in a console-less cmd, so they must not be relied on
        self.assertNotIn("timeout", s)
        self.assertNotIn("ping", s)

    def test_wait_s_is_the_iteration_cap(self):
        self.assertIn("if %_i% GEQ 7 ", update.windows_dir_swap_script(r"C:\a", r"C:\n", relaunch=False, pid=1, wait_s=7))
        self.assertIn("if %_i% GEQ 30 ", update.windows_dir_swap_script(r"C:\a", r"C:\n", relaunch=False, pid=1))  # default

    def test_rolls_back_if_copy_fails(self):
        # robocopy failure (errorlevel >= 8) or a missing exe restores the moved-aside
        # dir so the path is never left broken; relaunch only if an exe exists.
        s = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=True, pid=1)
        self.assertIn("if errorlevel 8 goto ccrollback", s)
        self.assertIn(r'move /Y "C:\app.old" "C:\app"', s)                       # restore old dir
        self.assertIn(r'if exist "C:\app\claude-continue.exe" start "" "C:\app\claude-continue.exe"', s)

    def test_aborts_without_merge_if_move_aside_fails(self):
        # if the move-aside didn't free the path (exe still present), skip robocopy so
        # we never merge the new tree onto the old one — un-updated beats half-merged.
        s = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=False, pid=1)
        self.assertIn(r'if exist "C:\app\claude-continue.exe" goto ccrelaunch', s)

    def test_no_relaunch(self):
        s = update.windows_dir_swap_script(r"C:\a", r"C:\n", relaunch=False, pid=1)
        self.assertNotIn('start ""', s)

    def test_clears_and_cleans_old_backup(self):
        s = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=True, pid=1)
        # the moved-aside dir is rmdir'd (best-effort) — cleanup_stale_update finishes
        # a locked one on the next launch.
        self.assertIn(r'rmdir /S /Q "C:\app.old"', s)


def _make_onedir_zip(d, *, with_exe=True):
    """A minimal one-dir release zip: top-level claude-continue/ (+ exe + _internal)."""
    src = os.path.join(d, "tree", "claude-continue")
    os.makedirs(os.path.join(src, "_internal"))
    if with_exe:
        with open(os.path.join(src, "claude-continue.exe"), "w") as f:
            f.write("newexe")
    with open(os.path.join(src, "_internal", "python311.dll"), "w") as f:
        f.write("dll")
    zip_path = os.path.join(d, "update.zip")
    root = os.path.join(d, "tree")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for base, _, files in os.walk(root):
            for fn in files:
                full = os.path.join(base, fn)
                zf.write(full, os.path.relpath(full, root))
    return zip_path


class TestApplyWindowsDir(unittest.TestCase):
    def _install(self, d):
        install = os.path.join(d, "install")
        os.makedirs(install)
        exe = os.path.join(install, "claude-continue.exe")
        open(exe, "w").close()
        return install, exe

    def test_launches_helper_with_no_window_kwargs(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        _, exe = self._install(d)
        zip_path = _make_onedir_zip(d)
        tmp = os.path.join(d, "work")
        os.makedirs(tmp)
        with mock.patch("claude_continue.update.osenv.no_window_kwargs", return_value={"creationflags": 0x08000000}) as nwk, \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update._allow_foreground_handoff"), \
             mock.patch("claude_continue.update.subprocess.Popen") as popen:
            update._apply_windows_dir(zip_path, tmp, relaunch=True)
        nwk.assert_called()                                 # CREATE_NO_WINDOW, not DETACHED
        argv, kwargs = popen.call_args
        self.assertEqual(argv[0][:2], ["cmd", "/c"])        # cmd /c <script>
        self.assertEqual(kwargs.get("creationflags"), 0x08000000)
        with open(argv[0][2], "rb") as f:                   # binary: see the real bytes
            raw = f.read()
        self.assertIn(b"robocopy", raw)                     # the real dir-swap script was written
        self.assertIn(b"\r\n", raw)                         # CRLF preserved (newline="")
        self.assertNotIn(b"\r\r\n", raw)                    # not doubled
        # the zip was extracted and validated before anything was spawned
        self.assertTrue(os.path.isfile(os.path.join(tmp, "extracted", "claude-continue", "claude-continue.exe")))

    def test_writes_pending_stamp_with_target_version(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        _, exe = self._install(d)
        zip_path = _make_onedir_zip(d)
        tmp = os.path.join(d, "work")
        os.makedirs(tmp)
        with mock.patch("claude_continue.update.osenv.no_window_kwargs", return_value={}), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update._allow_foreground_handoff"), \
             mock.patch("claude_continue.update.subprocess.Popen"):
            update._apply_windows_dir(zip_path, tmp, relaunch=True, target_version="v0.7.0")
        with open(exe + update._PENDING_SUFFIX) as f:
            self.assertEqual(f.read().strip(), "v0.7.0")

    def test_refuses_path_with_percent(self):
        # a '%' in the install folder triggers cmd var-expansion and would corrupt
        # the script -> fail cleanly (before extraction) rather than emit a broken .cmd.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        install = os.path.join(d, "50%done")
        os.makedirs(install)
        exe = os.path.join(install, "claude-continue.exe")
        open(exe, "w").close()
        with mock.patch("claude_continue.update.sys.executable", exe):
            with self.assertRaises(update.UpdateError):
                update._apply_windows_dir(os.path.join(d, "nope.zip"), os.path.join(d, "work"), relaunch=False)

    def test_rejects_zip_without_exe(self):
        # an archive that has claude-continue/ but no exe inside must be refused
        # before any swap is attempted.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        _, exe = self._install(d)
        zip_path = _make_onedir_zip(d, with_exe=False)
        tmp = os.path.join(d, "work")
        os.makedirs(tmp)
        with mock.patch("claude_continue.update.osenv.detect", return_value="windows"), \
             mock.patch("claude_continue.update.sys.executable", exe):
            with self.assertRaises(update.UpdateError):
                update._apply_windows_dir(zip_path, tmp, relaunch=False)


class TestCleanupStaleUpdate(unittest.TestCase):
    def test_removes_old_dir_when_frozen_on_windows(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        install = os.path.join(d, "install")
        os.makedirs(install)
        exe = os.path.join(install, "claude-continue.exe")
        open(exe, "w").close()
        old = install + ".old"            # the swap leaves a <install>.old DIRECTORY
        os.makedirs(old)
        open(os.path.join(old, "marker"), "w").close()
        with mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            update.cleanup_stale_update()
        self.assertFalse(os.path.exists(old))  # leftover dir removed
        self.assertTrue(os.path.exists(exe))   # the live exe untouched

    def test_pending_stamp_warns_when_version_did_not_advance(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        exe = os.path.join(d, "claude-continue.exe")
        open(exe, "w").close()
        pending = exe + update._PENDING_SUFFIX
        with open(pending, "w") as f:
            f.write("9.9.9")  # we tried to install 9.9.9 but we're still on the old build
        with mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.__version__", "0.6.1"), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            warn = update.cleanup_stale_update()
        self.assertIsNotNone(warn)
        self.assertIn("9.9.9", warn)
        self.assertFalse(os.path.exists(pending))  # cleared so it warns at most once

    def test_pending_stamp_silent_when_version_advanced(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        exe = os.path.join(d, "claude-continue.exe")
        open(exe, "w").close()
        pending = exe + update._PENDING_SUFFIX
        with open(pending, "w") as f:
            f.write("0.6.1")  # the swap landed: we ARE 0.6.1 now -> success, no warning
        with mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.__version__", "0.6.1"), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            warn = update.cleanup_stale_update()
        self.assertIsNone(warn)
        self.assertFalse(os.path.exists(pending))  # stamp still cleared

    def test_reaps_stale_update_temp_dir(self):
        # a cc-update-* dir left by an interrupted apply_update is reaped next launch.
        # Isolate gettempdir so the production glob can't touch the real shared temp
        # (a foreign cc-update-* there, e.g. a live in-flight update, must be safe),
        # and prove the reap is scoped to cc-update-* by keeping a control dir.
        isolated = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, isolated, True)
        leftover = os.path.join(isolated, "cc-update-abc123")
        survivor = os.path.join(isolated, "unrelated-dir")
        os.mkdir(leftover)
        os.mkdir(survivor)
        exe = os.path.join(isolated, "claude-continue.exe")
        open(exe, "w").close()
        with mock.patch("claude_continue.update.tempfile.gettempdir", return_value=isolated), \
             mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            update.cleanup_stale_update()
        self.assertFalse(os.path.exists(leftover))   # cc-update-* reaped
        self.assertTrue(os.path.exists(survivor))    # unrelated dir untouched

    def test_noop_when_no_old_present(self):
        exe = os.path.join(tempfile.mkdtemp(), "claude-continue.exe")
        open(exe, "w").close()
        with mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            update.cleanup_stale_update()  # no .old -> silent no-op, no raise
        self.assertTrue(os.path.exists(exe))

    def test_does_not_delete_old_when_exe_missing(self):
        # a stranded rollback target: exe absent, the .old DIR is the only surviving copy
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        install = os.path.join(d, "install")  # not created (exe missing)
        exe = os.path.join(install, "claude-continue.exe")
        old = install + ".old"
        os.makedirs(old)
        with mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            update.cleanup_stale_update()
        self.assertTrue(os.path.exists(old))  # NEVER delete the only copy

    def test_locked_old_is_swallowed(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        install = os.path.join(d, "install")
        os.makedirs(install)
        exe = os.path.join(install, "claude-continue.exe")
        open(exe, "w").close()
        os.makedirs(install + ".old")
        with mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"), \
             mock.patch("claude_continue.update.shutil.rmtree", side_effect=OSError("locked")):
            update.cleanup_stale_update()  # OSError swallowed, never raises

    def test_noop_from_source(self):
        # not frozen -> nothing to clean, never raises
        with mock.patch("claude_continue.update.is_frozen", return_value=False):
            update.cleanup_stale_update()


class TestDigestVerify(unittest.TestCase):
    def _file(self, content=b"payload"):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "f.bin")
        with open(p, "wb") as f:
            f.write(content)
        return p

    def test_match_passes(self):
        p = self._file(b"payload")
        update._verify_digest(p, "sha256:" + hashlib.sha256(b"payload").hexdigest())  # no raise

    def test_mismatch_raises(self):
        with self.assertRaises(update.UpdateError):
            update._verify_digest(self._file(b"payload"), "sha256:" + "0" * 64)

    def test_missing_digest_raises(self):
        with self.assertRaises(update.UpdateError):
            update._verify_digest(self._file(), None)

    def test_unsupported_algo_raises(self):
        with self.assertRaises(update.UpdateError):
            update._verify_digest(self._file(), "md5:deadbeef")


class TestSslContext(unittest.TestCase):
    def test_context_has_cas(self):
        # the context update.check/_download use must trust real CAs
        ctx = update._ssl_context()
        self.assertGreater(ctx.cert_store_stats().get("x509_ca", 0), 0)

    def test_frozen_merges_system_bundle_and_still_verifies(self):
        # when frozen we additively merge the OS bundle even if defaults loaded
        # some CAs; the context must still trust real CAs (and never fewer)
        with mock.patch.object(update, "is_frozen", return_value=True):
            ctx = update._ssl_context()
        self.assertGreater(ctx.cert_store_stats().get("x509_ca", 0), 0)
        self.assertTrue(ctx.check_hostname)               # verification not weakened
        self.assertEqual(ctx.verify_mode, __import__("ssl").CERT_REQUIRED)

    @unittest.skipUnless(any(os.path.exists(p) for p in update._CA_FALLBACKS),
                         "no system CA bundle present")
    def test_fallback_bundle_loads_into_empty_context(self):
        # simulate the frozen-app case: a context that starts with zero CAs, then
        # gets the system bundle loaded — proves the fallback file is usable.
        import ssl
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty.cert_store_stats().get("x509_ca", 0), 0)
        path = next(p for p in update._CA_FALLBACKS if os.path.exists(p))
        empty.load_verify_locations(cafile=path)
        self.assertGreater(empty.cert_store_stats().get("x509_ca", 0), 0)


class TestUrlGuard(unittest.TestCase):
    def test_rejects_foreign_host(self):
        with self.assertRaises(update.UpdateError):
            update._check_url("https://evil.example.com/x.zip")

    def test_rejects_plain_http(self):
        with self.assertRaises(update.UpdateError):
            update._check_url("http://github.com/x.zip")

    def test_allows_github_hosts(self):
        update._check_url("https://github.com/MikkoNumminen/claude-continue/releases/download/v1/x.zip")
        update._check_url("https://objects.githubusercontent.com/x")
        update._check_url("https://release-assets.githubusercontent.com/x")


@unittest.skipUnless(shutil.which("ditto"), "needs ditto (macOS)")
class TestMacosSwap(unittest.TestCase):
    def test_replaces_bundle(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        # synthetic "new" app -> zip
        new_src = os.path.join(tmp, "src", "claude-continue.app", "Contents", "MacOS")
        os.makedirs(new_src)
        with open(os.path.join(new_src, "claude-continue"), "w") as f:
            f.write("newbin")
        zip_path = os.path.join(tmp, "new.zip")
        subprocess.run(["ditto", "-c", "-k", "--keepParent",
                        os.path.join(tmp, "src", "claude-continue.app"), zip_path], check=True)
        # existing installed bundle with a marker that must disappear
        bundle = os.path.join(tmp, "install", "claude-continue.app")
        os.makedirs(os.path.join(bundle, "Contents"))
        with open(os.path.join(bundle, "Contents", "old-marker"), "w") as f:
            f.write("OLD")

        out = update._apply_macos(zip_path, os.path.join(tmp, "work"), False, bundle)

        self.assertEqual(out, bundle)
        self.assertTrue(os.path.exists(os.path.join(bundle, "Contents", "MacOS", "claude-continue")))
        self.assertFalse(os.path.exists(os.path.join(bundle, "Contents", "old-marker")))
        self.assertFalse(os.path.exists(bundle + ".old"))


@unittest.skipUnless(shutil.which("ditto"), "needs ditto (macOS)")
class TestMacosRollback(unittest.TestCase):
    def test_install_failure_restores_old_bundle(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        new_src = os.path.join(tmp, "src", "claude-continue.app", "Contents", "MacOS")
        os.makedirs(new_src)
        with open(os.path.join(new_src, "claude-continue"), "w") as f:
            f.write("newbin")
        zip_path = os.path.join(tmp, "new.zip")
        subprocess.run(["ditto", "-c", "-k", "--keepParent",
                        os.path.join(tmp, "src", "claude-continue.app"), zip_path], check=True)
        bundle = os.path.join(tmp, "install", "claude-continue.app")
        os.makedirs(os.path.join(bundle, "Contents"))
        with open(os.path.join(bundle, "Contents", "old-marker"), "w") as f:
            f.write("OLD")

        real_run = subprocess.run
        calls = {"n": 0}

        def fake_run(args, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_run(args, **kw)        # let the extract succeed
            raise subprocess.CalledProcessError(1, args)  # fail the install ditto

        with mock.patch.object(update.subprocess, "run", fake_run):
            with self.assertRaises(update.UpdateError):
                update._apply_macos(zip_path, os.path.join(tmp, "work"), False, bundle)

        # old bundle is back, intact; no orphaned .old
        self.assertTrue(os.path.exists(os.path.join(bundle, "Contents", "old-marker")))
        self.assertFalse(os.path.exists(bundle + ".old"))


class TestApplyContract(unittest.TestCase):
    def test_failed_extract_raises_updateerror_not_raw(self):
        # honors apply_update's "raises UpdateError on any problem" contract, so
        # `claude-continue update --apply` reports cleanly instead of a traceback
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bundle = os.path.join(tmp, "install", "claude-continue.app")
        os.makedirs(bundle)  # exists, but extract will fail before any rename

        def fail_run(args, **kw):
            raise subprocess.CalledProcessError(1, args)  # ditto extract blows up

        with mock.patch.object(update.subprocess, "run", fail_run):
            with self.assertRaises(update.UpdateError):
                update._apply_macos("/nope.zip", os.path.join(tmp, "work"), False, bundle)
        self.assertTrue(os.path.isdir(bundle))            # untouched: extract failed first
        self.assertFalse(os.path.exists(bundle + ".old"))


if __name__ == "__main__":
    unittest.main()
