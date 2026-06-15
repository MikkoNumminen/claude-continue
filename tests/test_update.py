import json
import os
import shutil
import subprocess
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
