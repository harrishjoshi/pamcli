"""pam_cli.cli: argument rules and main() flows, with a mocked browser."""

import io
import logging
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout, suppress
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from pam_cli.cli import build_parser, main
from pam_cli.config import DEFAULT_TIMEOUT_MS, MAX_SESSION_HOURS

PAM_URL = "https://pam.example.com/login"


def _run_main(*argv):
    with patch("sys.argv", ["pamcli", *argv]):
        return main()


class ArgumentRulesTests(unittest.TestCase):
    def test_invalid_usage_exits_2(self):
        for argv in (
            ["--login"],  # actions are commands, not flags
            ["login", "discover"],
            ["bogus"],
            ["req", "--hou", "4"],  # abbreviations are disabled
            ["req", "--hours", "0"],
            ["req", "-H", str(MAX_SESSION_HOURS + 1)],
            ["req", "-i", " , "],  # no IPs: never fall back to the whole grid
            ["req", "-i", "10.0.0"],  # partial: the filter would match others
            ["req", "-i", "10.0.0.1,db-host"],
            ["req", "-H", "abc"],
            ["req", "-e", "UAT", "-i", "10.0.0.1"],  # one source of targets
            ["req", "-a", ""],  # blank would match every row
            ["req", "-a", "  "],
            ["req", "-r", ""],  # a reason is required
            *(
                [command, option, "x"]
                for command in ("login", "discover")
                for option in ("--env", "-i", "--account", "-r")
            ),
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as ctx,
            ):
                _run_main(*argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_short_and_long_request_options_match(self):
        long_form = ["request", "--ips", "10.0.0.1"]
        long_form += ["--account", "db", "--hours", "2", "--reason", "patching"]
        short_form = ["req", "-i", "10.0.0.1"]
        short_form += ["-a", "db", "-H", "2", "-r", "patching"]
        parser = build_parser()
        args = parser.parse_args(short_form)
        self.assertEqual(args, parser.parse_args(long_form))
        self.assertEqual(args.command, "request")
        self.assertEqual(args.reason, "patching")

    def test_help_and_version(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(_run_main(), 0)
        self.assertIn("usage: pamcli", out.getvalue())
        # Each command's help is on its own line, aligned (the alias used to wrap).
        self.assertRegex(out.getvalue(), r"\n    login +log in")
        self.assertRegex(out.getvalue(), r"\n    request \(req\) +log in")
        for argv, expected in (
            (["req", "-h"], "--env"),
            (["login", "-h"], "Log in and keep the Chromium window open"),
            (["discover", "-h"], "login_page_fields.txt"),
            (["setup", "-h"], "safe to run again"),
            (["-V"], "pamcli "),
        ):
            with (
                self.subTest(argv=argv),
                redirect_stdout(io.StringIO()) as out,
                self.assertRaises(SystemExit) as ctx,
            ):
                _run_main(*argv)
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn(expected, out.getvalue())

    def test_example_comments_line_up_with_the_option_help(self):
        for argv in ([], ["req", "-h"]):
            with (
                self.subTest(argv=argv),
                patch.dict(os.environ, {"COLUMNS": "80"}),
                redirect_stdout(io.StringIO()) as out,
                suppress(SystemExit),
            ):
                _run_main(*argv)
            lines = out.getvalue().splitlines()
            # Nothing wraps in a standard 80-column terminal.
            self.assertLessEqual(max(map(len, lines)), 78)
            option = next(line for line in lines if "show detailed logs" in line)
            column = option.index("show detailed logs")
            examples = lines[lines.index("examples:") + 1 :]
            examples = [line for line in examples if "# " in line]
            self.assertTrue(examples)
            for line in examples:
                self.assertEqual(line.index("# "), column, line)
                # Room between even the longest example and its comment.
                self.assertEqual(line[line.index("# ") - 3 : line.index("# ")], "   ")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.context = MagicMock()
        self.context.pages = []
        self.page = self._open_tab()
        self.context.new_page.side_effect = lambda: self.page
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        # Drop any real PAM settings from the shell, so the mocked flow can't
        # pick them up (e.g. take_env_password). Tests set their own values.
        for name in ("PAM_TIMEOUT", "PAM_PASSWORD", "PAM_USERNAME", "PAM_URL"):
            os.environ.pop(name, None)
        for target, kwargs in (
            ("resolve_url", {"return_value": PAM_URL}),
            ("check_reachable", {"return_value": None}),
            ("sync_playwright", {}),
            ("_open_browser", {"return_value": self.context}),
            ("do_login", {"return_value": "https://pam.example.com/home"}),
            ("request_access", {"return_value": False}),
            ("discover", {}),
        ):
            patcher = patch(f"pam_cli.cli.{target}", **kwargs)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        out = patch("sys.stdout", new_callable=io.StringIO)
        err = patch("sys.stderr", new_callable=io.StringIO)
        self.stdout, self.stderr = out.start(), err.start()
        self.addCleanup(out.stop)
        self.addCleanup(err.stop)

    def _open_tab(self):
        """A tab in the mocked context; waiting on it simulates the user
        closing it, which removes it from context.pages."""
        tab = MagicMock()
        tab.wait_for_event.side_effect = lambda *_a, **_k: self.context.pages.remove(
            tab
        )
        self.context.pages.append(tab)
        return tab

    def test_login_keeps_browser_open_until_closed(self):
        self.assertEqual(_run_main("login"), 0)
        self.do_login.assert_called_once_with(
            self.page, PAM_URL, DEFAULT_TIMEOUT_MS, None
        )
        self.request_access.assert_not_called()
        self.page.wait_for_event.assert_called_once_with("close", timeout=0)
        self.context.close.assert_called_once()
        self.assertIn(
            "Logged in — the Chromium window stays open on "
            "https://pam.example.com/home",
            self.stdout.getvalue(),
        )

    def test_request_passes_env_file_and_flags(self):
        env_config = MagicMock()
        self.request_access.return_value = True
        with patch("pam_cli.cli.load_environment", return_value=env_config) as load:
            code = _run_main("req", "-e", "UAT.abc", "-H", "2")
        self.assertEqual(code, 0)
        load.assert_called_once_with("UAT.abc")
        self.request_access.assert_called_once_with(
            self.page, env_config, None, None, 2, None, DEFAULT_TIMEOUT_MS
        )
        self.assertIn(
            "Access requested — the Chromium window shows Requests > Approved",
            self.stdout.getvalue(),
        )

    def test_env_password_is_gone_before_the_browser_starts(self):
        seen_by_browser = []
        self._open_browser.side_effect = lambda _p: (
            seen_by_browser.append(os.environ.get("PAM_PASSWORD")) or self.context
        )
        with patch.dict(os.environ, {"PAM_PASSWORD": "s3cret"}):
            self.assertEqual(_run_main("login"), 0)
        self.assertEqual(seen_by_browser, [None])
        self.assertEqual(self.do_login.call_args.args[3], "s3cret")

    def test_setup_installs_and_checks_chromium_without_pam_settings(self):
        # Runs right after installing, before PAM_URL is configured.
        order = []
        with (
            patch(
                "pam_cli.cli.offer_to_save",
                side_effect=lambda: order.append("offer to save settings"),
            ),
            patch(
                "pam_cli.cli._install_chromium",
                side_effect=lambda: order.append("install Chromium") or True,
            ),
        ):
            self.assertEqual(_run_main("setup"), 0)
        # Settings are offered first; Chromium is installed either way.
        self.assertEqual(order, ["offer to save settings", "install Chromium"])
        self._open_browser.assert_called_once()
        self.assertTrue(self._open_browser.call_args.kwargs["headless"])
        self._open_browser.return_value.close.assert_called_once_with()
        self.assertIn("Chromium is ready", self.stdout.getvalue())
        self.resolve_url.assert_not_called()
        self.check_reachable.assert_not_called()
        self.do_login.assert_not_called()

    def test_setup_fails_when_the_download_fails(self):
        with (
            patch("pam_cli.cli.offer_to_save"),
            patch("pam_cli.cli._install_chromium", return_value=False),
        ):
            self.assertEqual(_run_main("setup"), 1)
        self.assertIn("ERROR: could not download Chromium", self.stderr.getvalue())
        self._open_browser.assert_not_called()

    def test_setup_fails_when_chromium_does_not_start(self):
        self._open_browser.side_effect = PlaywrightError("missing libraries")
        with (
            patch("pam_cli.cli.offer_to_save"),
            patch("pam_cli.cli._install_chromium", return_value=True),
        ):
            self.assertEqual(_run_main("setup"), 1)
        self.assertIn("doesn't start (missing libraries)", self.stderr.getvalue())

    def test_discover_closes_browser_without_logging_in(self):
        self.assertEqual(_run_main("discover"), 0)
        self.discover.assert_called_once()
        self.do_login.assert_not_called()
        self.context.close.assert_called_once()

    def test_playwright_debug_variables_are_dropped_before_it_starts(self):
        # They'd make Playwright print what it types: the password and TOTP.
        seen = []
        self.sync_playwright.side_effect = lambda: (
            seen.append([os.environ.get(n) for n in ("DEBUG", "DEBUGP", "PWDEBUG")])
            or MagicMock()
        )
        os.environ.update(DEBUG="pw:protocol", DEBUGP="1", PWDEBUG="1")
        self.addCleanup(logging.getLogger("pam_cli").handlers.clear)
        self.assertEqual(_run_main("login"), 0)
        self.assertEqual(seen, [[None, None, None]])
        self.assertIn("Ignoring DEBUG, DEBUGP, PWDEBUG", self.stderr.getvalue())

    def test_errors_quoting_the_page_cant_control_the_terminal(self):
        self.do_login.side_effect = RuntimeError("moved to https://x/\x1b]0;hi\x07")
        self.assertEqual(_run_main("login"), 1)
        self.assertIn("moved to https://x/\\x1b]0;hi\\x07", self.stderr.getvalue())

    def test_discover_failure_is_an_error_not_a_traceback(self):
        self.discover.side_effect = PlaywrightError("page didn't load")
        self.assertEqual(_run_main("discover"), 1)
        self.assertIn("ERROR: page didn't load", self.stderr.getvalue())
        self.context.close.assert_called_once()

    def test_setup_problems_fail_before_the_browser_starts(self):
        # Each case: (name, argv, change to make, expected error). Everything
        # here must be caught before the portal is contacted or Chromium runs.
        cases = [
            (
                "bad env file",
                ["req", "-e", "UAT"],
                lambda: patch(
                    "pam_cli.cli.load_environment", side_effect=RuntimeError("boom")
                ),
                "ERROR: boom",
            ),
            (
                "no PAM_URL",
                ["login"],
                lambda: patch.object(
                    self.resolve_url, "side_effect", ValueError("PAM_URL is not set")
                ),
                "PAM_URL is not set",
            ),
            *(
                (
                    f"PAM_TIMEOUT={v}",
                    ["login"],
                    lambda v=v: patch.dict(os.environ, {"PAM_TIMEOUT": v}),
                    "PAM_TIMEOUT must be a number of seconds, at least 1",
                )
                # 0.0001 s would round to 0 ms, which Playwright takes as
                # "no timeout".
                for v in ("abc", "0", "0.0001", "-5", "inf", "nan")
            ),
        ]
        for name, argv, change, error in cases:
            with self.subTest(name), change():
                self.assertEqual(_run_main(*argv), 1)
                self.assertIn(error, self.stderr.getvalue())
        self.check_reachable.assert_not_called()  # env/URL/timeout come first
        self.sync_playwright.assert_not_called()

    def test_certificate_problem_gets_its_own_advice(self):
        self.check_reachable.return_value = (
            "TLS certificate could not be verified: unable to get local issuer"
        )
        self.assertEqual(_run_main("login"), 1)
        error = self.stderr.getvalue()
        self.assertIn(
            "ERROR: The portal's TLS certificate could not be verified", error
        )
        self.assertIn("certificate chain may be incomplete", error)
        self.assertIn("SSL_CERT_FILE", error)
        self.assertNotIn("VPN", error)  # not a network problem
        self.sync_playwright.assert_not_called()

    def test_network_errors_cant_control_the_terminal(self):
        self.check_reachable.return_value = "refused\x1b]0;title\x07"
        self.assertEqual(_run_main("login"), 1)
        self.assertIn("(refused\\x1b]0;title\\x07)", self.stderr.getvalue())

    def test_unreachable_portal_fails_before_the_browser_starts(self):
        self.check_reachable.return_value = "connection refused"
        self.assertEqual(_run_main("login"), 1)
        self.assertIn(
            "ERROR: The PAM portal could not be reached (connection refused)",
            self.stderr.getvalue(),
        )
        self.sync_playwright.assert_not_called()

    def test_waits_until_every_tab_is_closed(self):
        # e.g. the portal tab plus a leftover tab: closing the window
        # closes both, and pamcli must not exit after just the first.
        ssh_tab = self._open_tab()
        self.assertEqual(_run_main("login"), 0)
        self.page.wait_for_event.assert_called_once_with("close", timeout=0)
        ssh_tab.wait_for_event.assert_called_once_with("close", timeout=0)
        self.assertEqual(self.context.pages, [])

    def test_pam_timeout_sets_the_page_timeout(self):
        os.environ["PAM_TIMEOUT"] = "90"
        self.assertEqual(_run_main("login"), 0)
        self.assertEqual(self.do_login.call_args.args[2], 90_000)
        # Also the default for page actions that don't pass their own.
        self.context.set_default_timeout.assert_called_once_with(90_000)

    def test_browser_launch_failure_exits_1(self):
        self._open_browser.side_effect = PlaywrightError("can't run")
        self.assertEqual(_run_main("login"), 1)
        self.assertIn("could not start Chromium", self.stderr.getvalue())

    def test_login_failure_leaves_browser_open_for_inspection(self):
        self.do_login.side_effect = RuntimeError("bad creds")
        self.page.wait_for_event.side_effect = Exception("browser crashed")
        self.assertEqual(_run_main("login"), 1)
        self.assertIn("ERROR: bad creds", self.stderr.getvalue())
        self.page.wait_for_event.assert_called_once_with("close", timeout=0)
        self.context.close.assert_called_once()

    def test_verbose_flag_turns_on_debug_logs(self):
        logger = logging.getLogger("pam_cli")
        self.addCleanup(logger.handlers.clear)
        for argv, level in (
            (["login"], logging.INFO),
            (["-v", "login"], logging.DEBUG),
            (["login", "-v"], logging.DEBUG),
        ):
            with self.subTest(argv=argv):
                _run_main(*argv)
                self.assertEqual(logger.level, level)

    def test_keyboard_interrupt_exits_130(self):
        self._open_browser.side_effect = KeyboardInterrupt
        self.assertEqual(_run_main("login"), 130)
        self.assertIn("Cancelled.", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
