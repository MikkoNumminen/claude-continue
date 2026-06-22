import os
import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import winshortcut as ws


class TestPaths(unittest.TestCase):
    def test_start_menu_lnk_path_uses_appdata(self):
        with mock.patch.dict(os.environ, {"APPDATA": os.path.join("X:", "Roaming")}):
            p = ws.start_menu_lnk_path()
        self.assertTrue(p.startswith(os.path.join("X:", "Roaming")))
        self.assertIn("Start Menu", p)
        self.assertTrue(p.endswith("claude-continue.lnk"))


class TestPsScript(unittest.TestCase):
    def test_has_target_workdir_save(self):
        s = ws.powershell_create_shortcut_script(r"C:\sm\cc.lnk", r"D:\app\claude-continue.exe", r"D:\app")
        self.assertIn("CreateShortcut('C:\\sm\\cc.lnk')", s)
        self.assertIn("$s.TargetPath='D:\\app\\claude-continue.exe'", s)
        self.assertIn("$s.WorkingDirectory='D:\\app'", s)
        self.assertIn("$s.Save()", s)

    def test_escapes_single_quotes(self):
        # a path with a single quote must be doubled so it can't break the PS literal
        s = ws.powershell_create_shortcut_script(r"C:\a'b\cc.lnk", r"D:\o'k\claude-continue.exe", r"D:\o'k")
        self.assertIn("'C:\\a''b\\cc.lnk'", s)
        self.assertIn("'D:\\o''k\\claude-continue.exe'", s)


class TestEnsureRegistered(unittest.TestCase):
    def _patches(self, *, enabled=True, registered=None, lnk_exists=False):
        return [
            mock.patch.object(ws, "_enabled", return_value=enabled),
            mock.patch.object(ws, "_registered_target", return_value=registered),
            mock.patch.object(ws, "_set_app_paths"),
            mock.patch.object(ws, "_powershell", return_value="powershell"),
            mock.patch.object(ws.os.path, "exists", return_value=lnk_exists),
            mock.patch.object(ws.os, "makedirs"),
            mock.patch.object(ws.osenv, "no_window_kwargs", return_value={}),
            mock.patch.object(ws.subprocess, "Popen"),
        ]

    def test_noop_when_not_enabled(self):
        with mock.patch.object(ws, "_enabled", return_value=False), \
             mock.patch.object(ws, "_set_app_paths") as sap, \
             mock.patch.object(ws.subprocess, "Popen") as popen:
            ws.ensure_registered(target="/app/claude-continue.exe")
        sap.assert_not_called()
        popen.assert_not_called()

    def test_registers_when_missing(self):
        ps = self._patches(registered=None, lnk_exists=False)
        with ps[0], ps[1], ps[2] as sap, ps[3], ps[4], ps[5], ps[6], ps[7] as popen:
            ws.ensure_registered(target="/app/claude-continue.exe")
        sap.assert_called_once_with("/app/claude-continue.exe", "/app")
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "powershell")
        self.assertIn("-Command", argv)
        self.assertIn("/app/claude-continue.exe", argv[-1])  # the script targets the live exe

    def test_noop_when_already_current(self):
        # already registered at this exact path AND the .lnk exists -> do nothing
        ps = self._patches(registered="/app/claude-continue.exe", lnk_exists=True)
        with ps[0], ps[1], ps[2] as sap, ps[3], ps[4], ps[5], ps[6], ps[7] as popen:
            ws.ensure_registered(target="/app/claude-continue.exe")
        sap.assert_not_called()
        popen.assert_not_called()

    def test_reregisters_when_target_changed(self):
        # the install moved (registered path != live exe) -> re-register, even though
        # the .lnk file still exists
        ps = self._patches(registered="/old/claude-continue.exe", lnk_exists=True)
        with ps[0], ps[1], ps[2] as sap, ps[3], ps[4], ps[5], ps[6], ps[7] as popen:
            ws.ensure_registered(target="/new/claude-continue.exe")
        sap.assert_called_once_with("/new/claude-continue.exe", "/new")
        popen.assert_called_once()

    def test_reregisters_when_lnk_missing_even_if_registry_current(self):
        # registry says current, but the user deleted the .lnk -> recreate it
        ps = self._patches(registered="/app/claude-continue.exe", lnk_exists=False)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7] as popen:
            ws.ensure_registered(target="/app/claude-continue.exe")
        popen.assert_called_once()

    def test_never_raises(self):
        # every step blows up -> ensure_registered swallows it all
        with mock.patch.object(ws, "_enabled", return_value=True), \
             mock.patch.object(ws, "_registered_target", side_effect=RuntimeError("boom")), \
             mock.patch.object(ws, "_set_app_paths", side_effect=RuntimeError("boom")), \
             mock.patch.object(ws.os.path, "exists", return_value=False), \
             mock.patch.object(ws.os, "makedirs"), \
             mock.patch.object(ws.osenv, "no_window_kwargs", return_value={}), \
             mock.patch.object(ws.subprocess, "Popen", side_effect=OSError("nope")):
            ws.ensure_registered(target="/app/claude-continue.exe")  # must not raise


class TestUnregister(unittest.TestCase):
    def test_removes_lnk_when_on_windows(self):
        # winreg import fails on the (Linux) test host, but that's swallowed; the .lnk
        # removal still runs and unregister never raises.
        with mock.patch.object(ws.osenv, "is_windows", return_value=True), \
             mock.patch.object(ws.os.path, "exists", return_value=True), \
             mock.patch.object(ws.os, "remove") as rm:
            ws.unregister()
        rm.assert_called_once()

    def test_noop_off_windows(self):
        with mock.patch.object(ws.osenv, "is_windows", return_value=False), \
             mock.patch.object(ws.os, "remove") as rm:
            ws.unregister()
        rm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
