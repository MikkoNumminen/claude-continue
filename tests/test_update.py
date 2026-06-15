import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
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
    ("claude-continue-windows-x64.exe", "https://x/win.exe"),
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
            self.assertEqual(update.asset_for_platform(_ASSETS), ("claude-continue-windows-x64.exe", "https://x/win.exe"))

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
            ("claude-continue-windows-x64.exe.sha256", "u1"),
            ("claude-continue-windows-x64.exe", "u2"),
        ]
        with _ForcePlatform("windows"):
            self.assertEqual(update.asset_for_platform(assets), ("claude-continue-windows-x64.exe", "u2"))


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


class TestWindowsSwapScript(unittest.TestCase):
    def test_contains_wait_copy_relaunch_selfdelete(self):
        s = update.windows_swap_script(r"C:\tmp\new.exe", r"C:\app\claude-continue.exe", 4321, relaunch=True)
        self.assertIn('PID eq 4321', s)            # wait for our process to exit
        self.assertIn('copy /Y', s)                # overwrite the exe
        self.assertIn(r'C:\app\claude-continue.exe', s)
        self.assertIn('start ""', s)               # relaunch
        self.assertIn('del "%~f0"', s)             # self-delete

    def test_no_relaunch(self):
        s = update.windows_swap_script("new.exe", "old.exe", 1, relaunch=False)
        self.assertNotIn("start ", s)

    def test_deletes_downloaded_exe(self):
        s = update.windows_swap_script(r"C:\tmp\new.exe", r"C:\app\cc.exe", 1, relaunch=True)
        self.assertIn(r'del "C:\tmp\new.exe"', s)  # don't leak the download


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
