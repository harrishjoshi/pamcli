"""Browser launch (visible, maximized, fresh incognito-style context), and
the one-time automatic install of Chromium / its Linux system libraries."""

import inspect
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import Browser, BrowserType
from playwright.sync_api import Error as PlaywrightError

from pam_cli.cli import (
    _confirm,
    _install_chromium,
    _install_system_deps,
    _open_browser,
)

_MISSING_DEPS = "Host system is missing dependencies to run browsers."
_DOESNT_EXIST = "BrowserType.launch: Executable doesn't exist at /root/.cache/x"


class OpenBrowserTests(unittest.TestCase):
    def test_launches_visible_maximized_with_a_fresh_context(self):
        playwright = MagicMock()
        launch = playwright.chromium.launch
        new_context = launch.return_value.new_context
        self.assertIs(_open_browser(playwright), new_context.return_value)
        # bind() checks each call against the real Playwright signature.
        launched = inspect.signature(BrowserType.launch).bind(
            None, *launch.call_args.args, **launch.call_args.kwargs
        )
        self.assertFalse(launched.arguments["headless"])
        self.assertIn("--start-maximized", launched.arguments["args"])
        context = inspect.signature(Browser.new_context).bind(
            None, *new_context.call_args.args, **new_context.call_args.kwargs
        )
        self.assertTrue(context.arguments["no_viewport"])  # page fills the window

    def test_headless_for_the_setup_check(self):
        playwright = MagicMock()
        _open_browser(playwright, headless=True)
        launch = playwright.chromium.launch
        launched = inspect.signature(BrowserType.launch).bind(
            None, *launch.call_args.args, **launch.call_args.kwargs
        )
        self.assertTrue(launched.arguments["headless"])
        self.assertEqual(launched.arguments["args"], [])  # nothing to maximize


class OpenBrowserRetryTests(unittest.TestCase):
    def _open(self, launch_results, deps_ok=True, chromium_ok=True):
        playwright = MagicMock()
        launch = playwright.chromium.launch
        launch.side_effect = launch_results
        with (
            patch(
                "pam_cli.cli._install_chromium", return_value=chromium_ok
            ) as chromium,
            patch("pam_cli.cli._install_system_deps", return_value=deps_ok) as deps,
        ):
            try:
                return _open_browser(playwright), chromium, deps, launch
            except PlaywrightError as exc:
                return exc, chromium, deps, launch

    def test_fresh_machine_installs_chromium_then_deps(self):
        browser = MagicMock()
        result, chromium, deps, _ = self._open(
            [PlaywrightError(_DOESNT_EXIST), PlaywrightError(_MISSING_DEPS), browser]
        )
        self.assertIs(result, browser.new_context.return_value)
        chromium.assert_called_once_with()
        deps.assert_called_once_with()

    def test_gives_up_when_an_install_fails(self):
        for error, kwargs in (
            (_DOESNT_EXIST, {"chromium_ok": False}),
            (_MISSING_DEPS, {"deps_ok": False}),
        ):
            with self.subTest(error):
                result, *_ = self._open([PlaywrightError(error)], **kwargs)
                self.assertIsInstance(result, PlaywrightError)

    def test_installs_each_fix_only_once(self):
        result, _, deps, launch = self._open([PlaywrightError(_MISSING_DEPS)] * 3)
        self.assertIsInstance(result, PlaywrightError)
        deps.assert_called_once_with()
        self.assertEqual(launch.call_count, 2)

    def test_unrelated_error_propagates(self):
        result, chromium, deps, _ = self._open([PlaywrightError("other failure")])
        self.assertIsInstance(result, PlaywrightError)
        chromium.assert_not_called()
        deps.assert_not_called()


@patch("pam_cli.cli.sys.platform", "linux")
class InstallSystemDepsTests(unittest.TestCase):
    def test_non_root_uses_sudo(self):
        with (
            patch("pam_cli.cli.os.geteuid", return_value=1000),
            patch("pam_cli.cli.shutil.which", return_value="/usr/bin/sudo"),
            patch("pam_cli.cli._confirm", return_value=True) as confirm,
            patch("pam_cli.cli.subprocess.run") as run,
            patch("builtins.print"),
        ):
            self.assertTrue(_install_system_deps())
        self.assertIn("sudo", confirm.call_args.args[0])  # asks first
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/sudo")
        self.assertEqual(command[2:], ["-m", "playwright", "install-deps", "chromium"])

    def test_returns_false_when_it_cannot_install(self):
        cases = {
            "no sudo": {"which": None},
            "declined": {"confirm": False},
            "install fails": {"run": subprocess.CalledProcessError(1, "x")},
        }
        for name, case in cases.items():
            with (
                self.subTest(name),
                patch("pam_cli.cli.os.geteuid", return_value=1000),
                patch(
                    "pam_cli.cli.shutil.which", return_value=case.get("which", "sudo")
                ),
                patch("pam_cli.cli._confirm", return_value=case.get("confirm", True)),
                patch("pam_cli.cli.subprocess.run", side_effect=case.get("run")),
                patch("builtins.print"),
            ):
                self.assertFalse(_install_system_deps())

    def test_confirm_needs_a_yes_at_a_terminal(self):
        for tty, answer, expected in (
            (True, "y", True),
            (True, "YES", True),
            (True, "", False),
            (True, "n", False),
            (False, "y", False),  # nobody to ask: no
        ):
            with (
                self.subTest(tty=tty, answer=answer),
                patch("pam_cli.cli.sys.stdin.isatty", return_value=tty),
                patch("builtins.input", return_value=answer),
            ):
                self.assertEqual(_confirm("ok? "), expected)

    def test_confirm_tolerates_hitting_the_end_of_the_input(self):
        with (
            patch("pam_cli.cli.sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=EOFError),
        ):
            self.assertFalse(_confirm("ok? "))

    def test_non_linux_is_a_no_op(self):
        with (
            patch("pam_cli.cli.sys.platform", "darwin"),
            patch("pam_cli.cli.subprocess.run") as run,
        ):
            self.assertFalse(_install_system_deps())
        run.assert_not_called()


class InstallChromiumTests(unittest.TestCase):
    def test_install_chromium_runs_the_playwright_installer(self):
        with (
            patch("pam_cli.cli.subprocess.run") as run,
            patch("builtins.print"),
        ):
            self.assertTrue(_install_chromium())
        command = run.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[2:], ["playwright", "install", "chromium"])

    def test_install_chromium_failure_is_reported_as_false(self):
        for error in (
            subprocess.CalledProcessError(1, "playwright"),
            OSError("offline"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("pam_cli.cli.subprocess.run", side_effect=error),
                patch("builtins.print"),
            ):
                self.assertFalse(_install_chromium())


if __name__ == "__main__":
    unittest.main()
