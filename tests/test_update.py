import hashlib
import json
import os
import shutil
import subprocess
import sys
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

    def test_malformed_asset_skipped_not_raised(self):
        # an asset missing name/url must be skipped, not KeyError out of check()
        # (its docstring promises "Never raises").
        with _ForcePlatform("windows"):
            info = update.check(opener=_opener({"tag_name": "v9.9.9", "assets": [
                {"label": "broken"},  # no name / browser_download_url
                {"name": "claude-continue-windows-x64.zip", "browser_download_url": "u", "digest": "sha256:x"},
            ]}), current="0.1.0")
        self.assertIsNone(info.error)
        self.assertEqual(info.asset_name, "claude-continue-windows-x64.zip")

    def test_non_dict_release_payload_reported_not_raised(self):
        # the GitHub API returning a non-object (e.g. a list) must surface via .error,
        # not raise AttributeError out of check().
        info = update.check(opener=_opener([]), current="0.1.0")
        self.assertIsNotNone(info.error)

    def test_null_release_body_reported_not_raised(self):
        # a JSON `null` body decodes to None -> must surface via .error, not raise
        # AssertionError (the loop breaks with no exception but no data).
        info = update.check(opener=_opener(None), current="0.1.0")
        self.assertIsNotNone(info.error)


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

    def test_runs_from_temp_not_install_cwd(self):
        # the helper must NOT hold the install dir as its CWD or Windows blocks the
        # move/rmdir of that very directory; it cd's to %TEMP% first.
        s = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=True, pid=1)
        self.assertIn('cd /d "%TEMP%"', s)

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
        # success skips the rollback block by jumping to :ccok (NOT falling into it)
        self.assertIn(r'if exist "C:\app\claude-continue.exe" goto ccok', s)
        self.assertIn(":ccok", s)

    def test_falls_back_to_inplace_when_move_aside_fails(self):
        # if the move-aside didn't free the path (exe still present = the dir is held
        # open by another process), don't bail un-updated — route to the in-place
        # overwrite fallback. Pin: the guard sits between move-aside and the
        # rename-path robocopy, and jumps to :ccinplace.
        lines = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=False, pid=1).splitlines()
        move_aside = lines.index(r'move /Y "C:\app" "C:\app.old" >NUL 2>&1')
        guard = lines.index(r'if exist "C:\app\claude-continue.exe" goto ccinplace')
        robocopy = next(i for i, ln in enumerate(lines) if ln.startswith(r'robocopy "C:\new" "C:\app" /E /MOVE'))
        self.assertLess(move_aside, guard)   # guard follows the move-aside
        self.assertLess(guard, robocopy)     # and precedes the rename-path merge
        self.assertIn(":ccinplace", lines)

    def test_inplace_backs_up_before_overwriting(self):
        # the in-place fallback copies the held install to <dir>.old (backup) BEFORE
        # overwriting the new tree over it, so a partial overwrite can be restored.
        lines = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=False, pid=1).splitlines()
        tail = lines[lines.index(":ccinplace"):]
        backup = next(i for i, ln in enumerate(tail) if ln.startswith(r'robocopy "C:\app" "C:\app.old"'))
        overwrite = next(i for i, ln in enumerate(tail) if ln.startswith(r'robocopy "C:\new" "C:\app"'))
        self.assertLess(backup, overwrite)

    def test_inplace_aborts_overwrite_if_backup_fails(self):
        # if the backup robocopy fails (errorlevel >= 8) the in-place path must NOT
        # overwrite — relaunch the intact old install instead (no half-written tree).
        lines = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=False, pid=1).splitlines()
        tail = lines[lines.index(":ccinplace"):]
        first_guard = next(ln for ln in tail if ln.startswith("if errorlevel 8"))
        self.assertEqual(first_guard, "if errorlevel 8 goto ccrelaunch")

    def test_inplace_restores_old_files_on_overwrite_failure(self):
        # a failed overwrite restores the old files from the backup over the partial
        # tree, and refuses to relaunch a broken install.
        lines = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=False, pid=1).splitlines()
        self.assertIn("if errorlevel 8 goto ccinrestore", lines)
        rest = lines[lines.index(":ccinrestore"):]
        restore = next(i for i, ln in enumerate(rest) if ln.startswith(r'robocopy "C:\app.old" "C:\app"'))
        # the restore's OWN exit code must be checked (robocopy writes the root exe
        # before recursing, so an incomplete restore can leave the exe present yet the
        # tree mismatched) BEFORE the exe-presence guard — else a partial restore would
        # drop the backup and relaunch a bricked install.
        self.assertEqual(rest[restore + 1], "if errorlevel 8 goto ccend")
        self.assertEqual(rest[restore + 2], r'if not exist "C:\app\claude-continue.exe" goto ccend')

    def test_inplace_block_precedes_giveup(self):
        # the cap-give-up tail must stay swap-free (asserted elsewhere); the whole
        # in-place block therefore has to sit BEFORE :ccgiveup.
        lines = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=True, pid=1).splitlines()
        self.assertLess(lines.index(":ccinplace"), lines.index(":ccgiveup"))

    def test_rollback_never_nests_backup(self):
        # the restore `move` must be gated on the partial dir being cleared first —
        # otherwise Windows nests <dir>.old INSIDE the leftover dir, stranding the only
        # good copy. Pin: `if exist <dir> goto ccrelaunch` sits between the rollback
        # rmdir and the restore move.
        lines = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=False, pid=1).splitlines()
        rb_rmdir = lines.index(r'rmdir /S /Q "C:\app" >NUL 2>&1')
        guard = lines.index(r'if exist "C:\app" goto ccrelaunch')
        restore = lines.index(r'move /Y "C:\app.old" "C:\app" >NUL 2>&1')
        self.assertLess(rb_rmdir, guard)
        self.assertLess(guard, restore)

    def test_no_relaunch(self):
        s = update.windows_dir_swap_script(r"C:\a", r"C:\n", relaunch=False, pid=1)
        self.assertNotIn('start ""', s)

    def test_clears_and_cleans_old_backup(self):
        s = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=True, pid=1)
        # rmdir of <dir>.old appears three times: clear-stale before move-aside, the
        # rename-path backup drop on success/restore, and the in-place backup drop.
        self.assertEqual(s.count(r'rmdir /S /Q "C:\app.old"'), 3)

    def test_cap_gives_up_without_swap_or_relaunch(self):
        # when the wait cap expires with our PID STILL alive, jump to :ccgiveup and exit
        # — never swap (the running app locks its dir) or relaunch (spawn a 2nd instance).
        lines = update.windows_dir_swap_script(r"C:\app", r"C:\new", relaunch=True, pid=1, wait_s=5).splitlines()
        cap = next(i for i, ln in enumerate(lines) if ln.startswith("if %_i% GEQ 5 "))
        self.assertIn("goto ccgiveup", lines[cap])           # cap -> give up (not ccswap)
        self.assertLess(cap, lines.index(":ccswap"))         # the cap decision precedes the swap
        tail = "\n".join(lines[lines.index(":ccgiveup"):])   # the give-up path to EOF
        self.assertNotIn("robocopy", tail)
        self.assertNotIn("move /Y", tail)
        self.assertNotIn('start ""', tail)


@unittest.skipUnless(os.name == "nt", "exercises the real Windows swap .cmd via cmd.exe")
class TestWindowsDirSwapScriptLive(unittest.TestCase):
    """Run the actual generated .cmd against real temp dirs: the rename path on a free
    install dir, and the in-place fallback on a HELD-open one (a process whose CWD is
    the install dir blocks the rename exactly as an Explorer window or AV scan would).
    Validates the swap end-to-end, not just the script text."""

    def _make_trees(self, d):
        install = os.path.join(d, "app")
        os.makedirs(os.path.join(install, "_internal"))
        with open(os.path.join(install, "claude-continue.exe"), "w") as f:
            f.write("OLD")
        with open(os.path.join(install, "_internal", "lib.dll"), "w") as f:
            f.write("OLDDLL")
        with open(os.path.join(install, "ORPHAN.txt"), "w") as f:  # an old-only marker file
            f.write("orphan")
        new = os.path.join(d, "new", "claude-continue")
        os.makedirs(os.path.join(new, "_internal"))
        with open(os.path.join(new, "claude-continue.exe"), "w") as f:
            f.write("NEW")
        with open(os.path.join(new, "_internal", "lib.dll"), "w") as f:
            f.write("NEWDLL")
        return install, new

    def _run_script(self, d, install, new):
        # pid 999999 doesn't exist -> tasklist reports no match -> the wait loop falls
        # straight through to the swap (verified: tasklist returns errorlevel 0 here).
        script = update.windows_dir_swap_script(install, new, relaunch=False, pid=999999, wait_s=5)
        sp = os.path.join(d, "swap.cmd")
        with open(sp, "w", newline="") as f:
            f.write(script)
        subprocess.run(["cmd", "/c", sp], cwd=tempfile.gettempdir(), timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()

    def _hold_dir(self, install):
        # Hold the install dir open the way Explorer / an AV scan does — as a live
        # process's CURRENT DIRECTORY. That blocks renaming the dir but adds NO
        # unreadable file inside it, so the in-place backup robocopy can still copy the
        # tree (an open file handle inside the dir can fail that backup under some
        # Python file-share modes). The child prints "R" once its CWD is set, so the
        # lock is established before we proceed (no startup race); killed on cleanup.
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import os,sys,time; os.chdir(sys.argv[1]); sys.stdout.write('R'); "
             "sys.stdout.flush(); time.sleep(120)", install],
            stdout=subprocess.PIPE)

        def _stop():
            holder.kill()
            try:
                holder.wait(timeout=5)
            except Exception:
                pass

        self.addCleanup(_stop)
        holder.stdout.read(1)  # block until the CWD lock is established
        return holder

    def test_rename_path_swaps_a_free_install(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        install, new = self._make_trees(d)
        self._run_script(d, install, new)
        self.assertEqual(self._read(install, "claude-continue.exe"), "NEW")       # new tree landed
        self.assertEqual(self._read(install, "_internal", "lib.dll"), "NEWDLL")
        # clean replace (atomic rename + robocopy /MOVE): the old-only orphan is gone,
        # and the .old backup was dropped.
        self.assertFalse(os.path.exists(os.path.join(install, "ORPHAN.txt")))
        self.assertFalse(os.path.exists(install + ".old"))

    def test_inplace_path_swaps_a_held_install(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        install, new = self._make_trees(d)
        self._hold_dir(install)  # dir held open -> move-aside fails -> in-place path
        # sanity: the hold really does block the rename, else we'd silently be
        # re-testing the rename path.
        with self.assertRaises(OSError):
            os.rename(install, install + ".probe")
        self._run_script(d, install, new)
        self.assertEqual(self._read(install, "claude-continue.exe"), "NEW")       # overwrote despite the lock
        self.assertEqual(self._read(install, "_internal", "lib.dll"), "NEWDLL")
        # the in-place overwrite does NOT purge, so the old-only orphan survives —
        # which also proves the in-place path ran (rename would have dropped it).
        self.assertTrue(os.path.exists(os.path.join(install, "ORPHAN.txt")))

    def test_inplace_restores_old_install_when_overwrite_fails(self):
        # drive the in-place path (held dir), then force the OVERWRITE robocopy to fail
        # deterministically by pointing its source at a non-existent tree (robocopy
        # exit 16). The script must back up, fail the overwrite, restore the old files
        # from the backup, and leave the original install intact — never a half-tree.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        install, _ = self._make_trees(d)
        bad_new = os.path.join(d, "missing", "claude-continue")  # source doesn't exist -> robocopy 16
        self._hold_dir(install)
        with self.assertRaises(OSError):
            os.rename(install, install + ".probe")          # confirm the dir is held
        self._run_script(d, install, bad_new)
        # overwrite failed -> restore ran -> the old install is intact (not bricked).
        self.assertEqual(self._read(install, "claude-continue.exe"), "OLD")
        self.assertEqual(self._read(install, "_internal", "lib.dll"), "OLDDLL")


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
        # the helper must run from %TEMP%, NOT the install dir, or Windows blocks the
        # move/rmdir of that very directory (it would be a live process's CWD).
        self.assertEqual(kwargs.get("cwd"), tempfile.gettempdir())
        with open(argv[0][2], "rb") as f:                   # binary: see the real bytes
            raw = f.read()
        self.assertIn(b"robocopy", raw)                     # the real dir-swap script was written
        self.assertIn(b'cd /d "%TEMP%"', raw)               # belt-and-suspenders CWD escape
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

    def test_no_pending_stamp_when_helper_spawn_fails(self):
        # the stamp is written only AFTER the helper spawns; a failed spawn must leave
        # NO stamp (else cleanup_stale_update shows a false "didn't complete" warning
        # even though the running install was never touched).
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        _, exe = self._install(d)
        zip_path = _make_onedir_zip(d)
        tmp = os.path.join(d, "work")
        os.makedirs(tmp)
        with mock.patch("claude_continue.update.osenv.no_window_kwargs", return_value={}), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.subprocess.Popen", side_effect=OSError("nope")):
            with self.assertRaises(update.UpdateError):
                update._apply_windows_dir(zip_path, tmp, relaunch=True, target_version="v9.9.9")
        self.assertFalse(os.path.exists(exe + update._PENDING_SUFFIX))

    def test_refuses_path_with_percent(self):
        # a '%' in the install folder triggers cmd var-expansion and would corrupt the
        # script -> fail cleanly via the path guard. Use a VALID zip so the ONLY thing
        # that can raise is the guard (a missing/bad zip would also raise UpdateError,
        # masking a regression that dropped the guard).
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        install = os.path.join(d, "50%done")
        os.makedirs(install)
        exe = os.path.join(install, "claude-continue.exe")
        open(exe, "w").close()
        zip_path = _make_onedir_zip(d)
        tmp = os.path.join(d, "work")
        os.makedirs(tmp)
        with mock.patch("claude_continue.update.sys.executable", exe):
            with self.assertRaisesRegex(update.UpdateError, r"app folder|characters that would break"):
                update._apply_windows_dir(zip_path, tmp, relaunch=False)

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


class TestSafeExtract(unittest.TestCase):
    def _zip_with(self, d, arcname):
        zp = os.path.join(d, "evil.zip")
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(arcname, "x")
        return zp

    def test_rejects_parent_traversal(self):
        # a member that escapes the target via ../ must be refused before extraction
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        zp = self._zip_with(d, "../escape.txt")
        with zipfile.ZipFile(zp) as zf:
            with self.assertRaises(update.UpdateError):
                update._safe_extract(zf, dest)
        self.assertFalse(os.path.exists(os.path.join(d, "escape.txt")))  # nothing written outside dest

    def test_rejects_deeper_traversal(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        zp = self._zip_with(d, "../../escape.txt")
        with zipfile.ZipFile(zp) as zf:
            with self.assertRaises(update.UpdateError):
                update._safe_extract(zf, dest)

    def test_allows_normal_nested_member(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        zp = self._zip_with(d, "claude-continue/_internal/python311.dll")
        with zipfile.ZipFile(zp) as zf:
            update._safe_extract(zf, dest)  # no raise
        self.assertTrue(os.path.isfile(os.path.join(dest, "claude-continue", "_internal", "python311.dll")))


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

    def test_reaps_OLD_stale_update_temp_dir(self):
        # an OLD cc-update-* dir left by an interrupted apply_update is reaped next
        # launch. Isolate gettempdir so the production glob can't touch the real shared
        # temp, and keep a control dir to prove the reap is scoped to cc-update-*.
        isolated = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, isolated, True)
        leftover = os.path.join(isolated, "cc-update-abc123")
        survivor = os.path.join(isolated, "unrelated-dir")
        os.mkdir(leftover)
        os.mkdir(survivor)
        os.utime(leftover, (0, 0))  # make it ancient so the age-guard reaps it
        exe = os.path.join(isolated, "claude-continue.exe")
        open(exe, "w").close()
        with mock.patch("claude_continue.update.tempfile.gettempdir", return_value=isolated), \
             mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            update.cleanup_stale_update()
        self.assertFalse(os.path.exists(leftover))   # old cc-update-* reaped
        self.assertTrue(os.path.exists(survivor))    # unrelated dir untouched

    def test_does_NOT_reap_a_fresh_cc_update_dir(self):
        # a FRESH cc-update-* may be a concurrent instance's in-flight swap source —
        # the age-guard must leave it alone (reaping it would race that update).
        isolated = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, isolated, True)
        inflight = os.path.join(isolated, "cc-update-live99")
        os.mkdir(inflight)  # mtime = now
        exe = os.path.join(isolated, "claude-continue.exe")
        open(exe, "w").close()
        with mock.patch("claude_continue.update.tempfile.gettempdir", return_value=isolated), \
             mock.patch("claude_continue.update.is_frozen", return_value=True), \
             mock.patch("claude_continue.update.sys.executable", exe), \
             mock.patch("claude_continue.update.osenv.detect", return_value="windows"):
            update.cleanup_stale_update()
        self.assertTrue(os.path.exists(inflight))    # fresh in-flight dir preserved

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


class TestMacosRelauncherCap(unittest.TestCase):
    def test_wait_loop_is_capped(self):
        # the relaunch helper must not wait forever on a recycled/never-dying PID
        with mock.patch("claude_continue.update.subprocess.Popen") as popen, \
             mock.patch("claude_continue.update.osenv.detached_popen_kwargs", return_value={}):
            update._spawn_macos_relauncher("/Applications/claude-continue.app")
        path = popen.call_args[0][0][1]  # Popen(["/bin/sh", <path>], ...)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path) as f:
            script = f.read()
        self.assertIn("kill -0", script)
        self.assertIn("[ $i -lt 100 ]", script)  # bounded loop, not an infinite kill -0 wait


class TestMacosRollbackFailure(unittest.TestCase):
    def test_failed_restore_surfaces_accurate_error(self):
        # if the rollback's restore (rename backup -> bundle) ALSO fails, surface an
        # UpdateError naming the stranded backup, not a raw OSError that hides the cause.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        work = os.path.join(tmp, "work")
        os.makedirs(os.path.join(work, "claude-continue.app"))  # the "extracted" new app
        bundle = os.path.join(tmp, "install", "claude-continue.app")
        os.makedirs(os.path.join(bundle, "Contents"))
        n = {"run": 0}

        def fake_run(args, **kw):
            n["run"] += 1
            if n["run"] == 1:
                return subprocess.CompletedProcess(args, 0)   # ditto extract: ok (no-op)
            raise subprocess.CalledProcessError(1, args)       # ditto install: fail -> rollback

        real_rename = os.rename

        def fake_rename(src, dst):
            if str(src).endswith(".old"):                      # the rollback RESTORE
                raise OSError("restore denied")
            return real_rename(src, dst)                       # the move-aside: real

        with mock.patch.object(update.subprocess, "run", fake_run), \
             mock.patch.object(update.os, "rename", fake_rename):
            with self.assertRaises(update.UpdateError) as cm:
                update._apply_macos("/nope.zip", work, False, bundle)
        msg = str(cm.exception)
        self.assertIn(".old", msg)        # names the stranded backup
        self.assertIn("manually", msg)


if __name__ == "__main__":
    unittest.main()
