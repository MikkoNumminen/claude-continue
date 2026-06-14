import os
import plistlib
import unittest
from pathlib import Path
from unittest import mock

import _support  # noqa: F401

from claude_continue import launchd


class TestRenderPlist(unittest.TestCase):
    def test_parses_and_has_expected_keys(self):
        xml = launchd.render_plist(
            ["/usr/local/bin/claude-continue", "watch", "--buffer", "120"],
            path_value="/opt/node/bin:/usr/bin:/bin",
            stdout="/tmp/out.log",
            stderr="/tmp/err.log",
        )
        d = plistlib.loads(xml.encode())
        self.assertEqual(d["Label"], "com.mikko.claude-continue")
        self.assertEqual(d["ProgramArguments"],
                         ["/usr/local/bin/claude-continue", "watch", "--buffer", "120"])
        self.assertTrue(d["RunAtLoad"])
        self.assertEqual(d["KeepAlive"], {"Crashed": True, "SuccessfulExit": False})
        self.assertEqual(d["ThrottleInterval"], 30)
        self.assertEqual(d["EnvironmentVariables"]["PATH"], "/opt/node/bin:/usr/bin:/bin")
        self.assertEqual(d["ProcessType"], "Background")

    def test_xml_escapes_args(self):
        xml = launchd.render_plist(
            ["/bin/cc", "watch", "--exec", 'claude -p "go" & wait <x>'],
            path_value="/usr/bin",
        )
        d = plistlib.loads(xml.encode())  # must still parse
        self.assertIn('claude -p "go" & wait <x>', d["ProgramArguments"])

    def test_node_path_includes_node_dir(self):
        # uses the real environment's node if present; otherwise still well-formed
        pv = launchd.node_path_value()
        self.assertIn("/usr/bin", pv)

    def test_node_path_extra_first_and_deduped(self):
        with mock.patch("claude_continue.launchd.shutil.which", return_value="/fake/nvm/bin/node"), \
             mock.patch("claude_continue.launchd.os.path.exists", return_value=False):
            pv = launchd.node_path_value(extra="/usr/bin")
        parts = pv.split(":")
        self.assertEqual(parts[0], "/usr/bin")          # extra goes first
        self.assertEqual(parts.count("/usr/bin"), 1)    # and is not duplicated
        self.assertIn("/fake/nvm/bin", parts)           # node's dir is included

    def test_xml_escape_rejects_control_chars(self):
        with self.assertRaises(ValueError):
            launchd.render_plist(["/bin/cc", "watch", "--text", "a\x07b"], path_value="/usr/bin")

    def test_is_volatile_node_dir(self):
        self.assertTrue(launchd.is_volatile_node_dir("/Users/x/.nvm/versions/node/v22.0.0/bin/node"))
        self.assertFalse(launchd.is_volatile_node_dir("/opt/homebrew/bin/node"))


class TestTemplateNoDrift(unittest.TestCase):
    def test_embedded_matches_reference_file(self):
        ref = Path(__file__).resolve().parents[1] / "templates" / "com.mikko.claude-continue.plist.tmpl"
        self.assertEqual(ref.read_text(), launchd.PLIST_TEMPLATE.template)


if __name__ == "__main__":
    unittest.main()
