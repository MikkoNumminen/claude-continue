import unittest
from unittest import mock

import _support  # noqa: F401

from claude_continue import cli


def _non_none(d):
    return {k: v for k, v in d.items() if v is not None}


class TestUninstallApp(unittest.TestCase):
    def test_app_flag_routes_to_complete_removal(self):
        from claude_continue import selfremove
        args = cli.build_parser().parse_args(["uninstall", "--app"])
        summary = {"agent_removed": True, "deleted": [], "bundle": "/Applications/x.app", "frozen": True}
        with mock.patch.object(selfremove, "remove", return_value=summary) as rm:
            rc = cli.cmd_uninstall(args)
        self.assertEqual(rc, 0)
        rm.assert_called_once()
        self.assertTrue(rm.call_args.kwargs.get("purge_config"))

    def test_plain_uninstall_does_not_self_remove(self):
        from claude_continue import scheduler, selfremove
        args = cli.build_parser().parse_args(["uninstall"])
        with mock.patch.object(scheduler, "uninstall", return_value=True), \
             mock.patch.object(selfremove, "remove") as rm:
            rc = cli.cmd_uninstall(args)
        self.assertEqual(rc, 0)
        rm.assert_not_called()


class TestUpdateCommand(unittest.TestCase):
    def _run(self, info, apply=False):
        from claude_continue import update
        args = cli.build_parser().parse_args(["update"] + (["--apply"] if apply else []))
        with mock.patch.object(update, "check", return_value=info):
            return cli.cmd_update(args)

    def test_up_to_date_returns_zero(self):
        from claude_continue.update import UpdateInfo
        self.assertEqual(self._run(UpdateInfo("0.5.1", "v0.5.1", False, None, None)), 0)

    def test_error_returns_one(self):
        from claude_continue.update import UpdateInfo
        self.assertEqual(self._run(UpdateInfo("0.5.1", None, False, None, None, error="net")), 1)

    def test_newer_from_source_reports_without_applying(self):
        from claude_continue import update
        from claude_continue.update import UpdateInfo
        info = UpdateInfo("0.5.1", "v0.6.0", True, "a.zip", "https://x")
        args = cli.build_parser().parse_args(["update", "--apply"])
        with mock.patch.object(update, "check", return_value=info), \
             mock.patch.object(update, "is_frozen", return_value=False), \
             mock.patch.object(update, "apply_update") as ap:
            rc = cli.cmd_update(args)
        self.assertEqual(rc, 0)
        ap.assert_not_called()  # from source -> never auto-applies

    def test_newer_frozen_with_apply_calls_apply_update(self):
        from claude_continue import update
        from claude_continue.update import UpdateInfo
        info = UpdateInfo("0.5.1", "v0.6.0", True, "a.zip", "https://x")
        args = cli.build_parser().parse_args(["update", "--apply"])
        with mock.patch.object(update, "check", return_value=info), \
             mock.patch.object(update, "is_frozen", return_value=True), \
             mock.patch.object(update, "apply_update", return_value="/Applications/x.app") as ap:
            rc = cli.cmd_update(args)
        self.assertEqual(rc, 0)
        ap.assert_called_once()


class TestParser(unittest.TestCase):
    def test_each_subcommand_parses(self):
        p = cli.build_parser()
        for cmd in ["status", "watch", "once", "fire", "install", "uninstall"]:
            args = p.parse_args([cmd])
            self.assertTrue(hasattr(args, "func"))

    def test_no_subcommand_errors(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])


class TestOverridesRoundTrip(unittest.TestCase):
    def test_install_flags_reconstruct_to_watch(self):
        p = cli.build_parser()
        install_args = p.parse_args([
            "install",
            "--exec", "claude -p 'go' --permission-mode bypassPermissions",
            "--buffer", "120",
            "--no-skip-busy",
            "--filter", "claude,✳",
            "--all",
            "--every", "5",
            "--anchor", "06:00",
        ])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))

        # re-parse the reconstructed flags under `watch`
        watch_args = p.parse_args(["watch"] + argv)
        before = _non_none(cli.build_overrides(install_args))
        after = _non_none(cli.build_overrides(watch_args))
        self.assertEqual(before, after)

    def test_skip_busy_true_emits_flag(self):
        argv = cli.overrides_to_argv({"skip_busy": True})
        self.assertEqual(argv, ["--skip-busy"])

    def test_skip_busy_false_emits_negation(self):
        argv = cli.overrides_to_argv({"skip_busy": False})
        self.assertEqual(argv, ["--no-skip-busy"])

    def test_none_values_skipped(self):
        self.assertEqual(cli.overrides_to_argv({"buffer": None, "text": None}), [])

    def test_keystroke_flags_roundtrip(self):
        p = cli.build_parser()
        install_args = p.parse_args(["install", "--keystroke", "--window-title", "My Term"])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))
        self.assertIn("--keystroke", argv)
        self.assertIn("--window-title", argv)
        watch_args = p.parse_args(["watch"] + argv)
        self.assertTrue(watch_args.keystroke)
        self.assertEqual(watch_args.window_title, "My Term")

    def test_launchd_only_fields_not_emitted(self):
        self.assertEqual(cli.overrides_to_argv({"node_path": "/x", "log_path": "/y"}), [])

    def test_tmux_flags_roundtrip(self):
        p = cli.build_parser()
        install_args = p.parse_args(["install", "--tmux", "--tmux-busy-pattern", "Working…"])
        argv = cli.overrides_to_argv(cli.build_overrides(install_args))
        self.assertIn("--tmux", argv)
        self.assertIn("--tmux-busy-pattern", argv)
        watch_args = p.parse_args(["watch"] + argv)
        self.assertTrue(watch_args.tmux)
        self.assertEqual(watch_args.tmux_busy_pattern, "Working…")

    def test_tmux_true_emits_bare_flag_not_value(self):
        argv = cli.overrides_to_argv({"tmux": True})
        self.assertEqual(argv, ["--tmux"])  # not ["--tmux", "True"]


class TestFireCommand(unittest.TestCase):
    def test_fire_dry_run_calls_perform_with_dry_run(self):
        p = cli.build_parser()
        args = p.parse_args(["fire", "--dry-run", "--session", "Job"])
        with mock.patch("claude_continue.cli.action.perform", return_value=["Job"]) as m:
            rc = cli.cmd_fire(args)
        self.assertEqual(rc, 0)
        _, kwargs = m.call_args
        self.assertTrue(kwargs.get("dry_run"))

    def test_fire_real_calls_perform(self):
        p = cli.build_parser()
        args = p.parse_args(["fire"])
        with mock.patch("claude_continue.cli.action.perform", return_value=[]) as m:
            cli.cmd_fire(args)
        _, kwargs = m.call_args
        self.assertFalse(kwargs.get("dry_run"))


if __name__ == "__main__":
    unittest.main()
