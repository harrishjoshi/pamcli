"""pam_cli.auth: the login flow against a mocked page."""

import os
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.auth import (
    FAILURE_SCREENSHOT_PATH,
    TOTP_ATTEMPTS,
    _fail,
    _totp_accepted,
    dismiss_authorized_use_banner,
    do_login,
    is_logged_in,
    resolve_password,
    resolve_totp,
    resolve_url,
    resolve_username,
    take_env_password,
    wait_for_login_success,
    wait_for_totp_or_success,
)
from pam_cli.config import TEXT, TOTP_INPUT_SELECTOR

URL = "https://pam.example/login"


def _make_page(username="alice", password="pw", totp="123456"):
    """A page whose Username/Password/TOTP fields read back the given values."""
    page = MagicMock(url=URL)
    fields = {
        TEXT["username_label"]: MagicMock(**{"input_value.return_value": username}),
        TEXT["password_label"]: MagicMock(**{"input_value.return_value": password}),
    }
    page.get_by_label.side_effect = lambda label, **_: fields[label]
    page.locator.return_value.input_value.return_value = totp
    return page, fields


class DoLoginTests(unittest.TestCase):
    def setUp(self):
        for target, kwargs in (
            ("install_input_blocker", {}),
            ("remove_input_blocker", {}),
            ("dismiss_authorized_use_banner", {}),
            ("_totp_accepted", {"return_value": True}),
            ("wait_for_totp_or_success", {"return_value": "success"}),
            ("resolve_username", {"return_value": "alice"}),
            ("resolve_password", {"return_value": "pw"}),
            ("resolve_totp", {"return_value": "123456"}),
            ("write_private", {}),
        ):
            patcher = patch(f"pam_cli.auth.{target}", **kwargs)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        printer = patch("builtins.print")
        self.print = printer.start()
        self.addCleanup(printer.stop)

    def test_credentials_are_only_typed_on_pam_urls_site_over_https(self):
        for landed, typed in (
            ("https://pam.example/login?step=2", True),
            ("https://PAM.example:443/login", True),  # same site, spelled out
            ("http://pam.example/login", False),  # downgraded to http
            ("https://evil.example/login", False),  # redirected elsewhere
            ("https://pam.example:8443/login", False),  # another port
        ):
            with self.subTest(landed):
                page, fields = _make_page()
                page.url = landed
                if typed:
                    do_login(page, URL, 5000)
                else:
                    with self.assertRaisesRegex(
                        RuntimeError, "before the Username was typed; nothing was"
                    ):
                        do_login(page, URL, 5000)
                for field in fields.values():
                    self.assertEqual(field.press_sequentially.called, typed)

    def test_totp_is_not_typed_after_leaving_the_site(self):
        self.wait_for_totp_or_success.return_value = "totp"
        page, _ = _make_page()

        def moved():
            page.url = "https://evil.example/login"
            return "123456"

        self.resolve_totp.side_effect = moved
        with self.assertRaisesRegex(RuntimeError, "before the TOTP was typed"):
            do_login(page, URL, 5000)
        page.locator.return_value.press_sequentially.assert_not_called()
        page.locator.return_value.fill.assert_not_called()  # not even cleared

    def test_a_redirect_while_typing_stops_before_submitting(self):
        page, fields = _make_page()
        password = fields[TEXT["password_label"]]

        def redirected(*_args, **_kwargs):
            page.url = "https://evil.example/"

        password.press_sequentially.side_effect = redirected
        with self.assertRaisesRegex(
            RuntimeError, "while the Password was being typed; nothing was submitted"
        ):
            do_login(page, URL, 5000)
        page.get_by_role.return_value.dispatch_event.assert_not_called()

    def test_totp_login_types_verifies_and_submits(self):
        self.wait_for_totp_or_success.return_value = "totp"
        page, fields = _make_page()
        do_login(page, URL, 5000)

        page.locator.assert_called_once_with(TOTP_INPUT_SELECTOR)
        # Each field gets real keystrokes and then loses focus, so the page
        # saves its value before the form is submitted.
        for field, value in (
            (fields[TEXT["username_label"]], "alice"),
            (fields[TEXT["password_label"]], "pw"),
            (page.locator.return_value, "123456"),
        ):
            field.press_sequentially.assert_called_once_with(
                value, delay=50, timeout=5000
            )
            field.blur.assert_called_once_with(timeout=5000)
        # Buttons are pressed directly, past the layer that blocks manual input.
        self.assertEqual(
            page.get_by_role.return_value.dispatch_event.call_args_list,
            [call("click", timeout=5000)] * 2,
        )
        self._totp_accepted.assert_called_once()
        self.remove_input_blocker.assert_called_once_with(page)

    def test_wrong_totp_code_is_asked_for_again(self):
        self.wait_for_totp_or_success.return_value = "totp"
        self._totp_accepted.side_effect = [False, True]
        page, _ = _make_page()
        do_login(page, URL, 5000)
        self.assertEqual(self.resolve_totp.call_count, 2)
        self.assertIn(
            call("That code didn't work — try again.", file=ANY),
            self.print.call_args_list,
        )
        page.locator.return_value.fill.assert_called_with("", timeout=5000)  # cleared

    def test_gives_up_after_three_rejected_codes(self):
        self.wait_for_totp_or_success.return_value = "totp"
        self._totp_accepted.return_value = False
        page, _ = _make_page()
        with self.assertRaisesRegex(RuntimeError, "rejected 3 times"):
            do_login(page, URL, 5000)
        self.assertEqual(self.resolve_totp.call_count, TOTP_ATTEMPTS)
        self.write_private.assert_called_once()  # the failure screenshot

    def test_missing_login_form_stops_before_asking_for_credentials(self):
        page, fields = _make_page()
        fields[TEXT["username_label"]].wait_for.side_effect = PlaywrightTimeoutError(
            "t"
        )
        with self.assertRaisesRegex(RuntimeError, "Could not find the login form"):
            do_login(page, URL, 5000)
        self.resolve_username.assert_not_called()
        self.install_input_blocker.assert_not_called()

    def test_portal_not_opening_after_the_totp_code_fails(self):
        self.wait_for_totp_or_success.return_value = "totp"
        self._totp_accepted.side_effect = PlaywrightTimeoutError("t")
        page, _ = _make_page()
        with self.assertRaisesRegex(RuntimeError, "didn't open after the TOTP code"):
            do_login(page, URL, 5000)
        self.write_private.assert_called_once()  # the failure screenshot
        self.remove_input_blocker.assert_called_once_with(page)

    def test_failures_screenshot_and_remove_blocker(self):
        cases = {
            "username readback": ({"username": "other"}, None),
            "password readback": ({"password": "other"}, None),
            "totp readback": ({"totp": "000000"}, "totp"),
            "no totp or redirect": ({}, PlaywrightTimeoutError("t")),
        }
        for name, (page_kwargs, outcome) in cases.items():
            with self.subTest(name):
                self.remove_input_blocker.reset_mock()
                self.write_private.reset_mock()
                self.wait_for_totp_or_success.side_effect = (
                    outcome if isinstance(outcome, Exception) else None
                )
                self.wait_for_totp_or_success.return_value = outcome or "success"
                page, _ = _make_page(**page_kwargs)
                with self.assertRaisesRegex(RuntimeError, FAILURE_SCREENSHOT_PATH):
                    do_login(page, URL, 5000)
                self.write_private.assert_called_with(
                    Path(FAILURE_SCREENSHOT_PATH), page.screenshot.return_value
                )
                self.remove_input_blocker.assert_called_once_with(page)


class FailTests(unittest.TestCase):
    def test_a_failed_screenshot_does_not_hide_the_error(self):
        for problem in (PlaywrightError("page crashed"), OSError("read-only")):
            with self.subTest(type(problem).__name__):
                page = MagicMock()
                page.screenshot.side_effect = (
                    problem if isinstance(problem, PlaywrightError) else None
                )
                with patch(
                    "pam_cli.auth.write_private",
                    side_effect=problem if isinstance(problem, OSError) else None,
                ):
                    error = _fail(page, "Login didn't go through.")
                # The real message, without pointing at a screenshot that
                # wasn't saved.
                self.assertEqual(str(error), "Login didn't go through.")


@patch("pam_cli.polling.time.sleep")
class TotpAcceptedTests(unittest.TestCase):
    def _check(self, logged_in, field_visible):
        page, field = MagicMock(), MagicMock()
        field.is_visible.return_value = field_visible
        with (
            patch("pam_cli.auth.is_logged_in", return_value=logged_in),
            patch("pam_cli.polling.time.monotonic", side_effect=[0, 0, 99, 99]),
            patch("pam_cli.auth.wait_for_login_success") as wait,
        ):
            return _totp_accepted(page, field, 5000), wait

    def test_logged_in(self, _sleep):
        self.assertEqual(self._check(True, True)[0], True)

    def test_rejected_while_the_totp_field_is_still_shown(self, _sleep):
        accepted, wait = self._check(False, True)
        self.assertFalse(accepted)
        wait.assert_not_called()

    def test_page_moved_on_waits_for_the_portal(self, _sleep):
        accepted, wait = self._check(False, False)
        self.assertTrue(accepted)
        wait.assert_called_once()


class ResolveCredentialsTests(unittest.TestCase):
    def test_pam_url_rules(self):
        with patch.dict(os.environ, {"PAM_URL": URL}):
            self.assertEqual(resolve_url(), URL)
        for url, error in (
            ("  ", "PAM_URL is not set"),
            # Anything but https would send the password in clear text.
            ("http://pam.example/login", "must be an https:// URL"),
            ("pam.example/login", "must be an https:// URL"),
            ("https://", "must be an https:// URL"),
            ("https://pam.example/home/", "must be the portal's login page"),
            # Only the path and fragment count, not the host.
            ("https://login.pam.example/", "must be the portal's login page"),
            # The URL is logged, so it mustn't carry credentials.
            ("https://bob@pam.example/login", "must not contain a username"),
            ("https://bob:pw@pam.example/login", "must not contain a username"),
            # ...and that's checked before any error that would show the URL.
            ("http://bob:pw@pam.example/login", "must not contain a username"),
        ):
            with (
                self.subTest(url=url),
                patch.dict(os.environ, {"PAM_URL": url}),
                self.assertRaisesRegex(ValueError, error),
            ):
                resolve_url()

    def test_env_password_is_removed_from_the_environment(self):
        with patch.dict(os.environ, {"PAM_PASSWORD": "s3cret"}):
            self.assertEqual(take_env_password(), "s3cret")
            self.assertNotIn("PAM_PASSWORD", os.environ)
            self.assertIsNone(take_env_password())

    def test_username_from_env_or_prompt(self):
        # From PAM_USERNAME: shown, since there's no prompt.
        with (
            patch.dict(os.environ, {"PAM_USERNAME": "alice"}),
            patch("builtins.print") as print_,
        ):
            self.assertEqual(resolve_username(), "alice")
        print_.assert_called_once_with("PAM username: alice", flush=True)
        # Typed at the prompt: not repeated.
        with (
            patch.dict(os.environ, {"PAM_USERNAME": ""}),
            patch("builtins.input", return_value=" bob "),
            patch("builtins.print") as print_,
        ):
            self.assertEqual(resolve_username(), "bob")
        print_.assert_not_called()
        with (
            patch.dict(os.environ, {"PAM_USERNAME": ""}),
            patch("builtins.input", return_value="  "),
            self.assertRaisesRegex(ValueError, "cannot be empty"),
        ):
            resolve_username()

    def test_password_from_env_skips_prompt(self):
        with (
            patch("pam_cli.auth.getpass.getpass") as getpass,
            patch("builtins.print"),
        ):
            self.assertEqual(resolve_password("s3cret"), "s3cret")
        getpass.assert_not_called()

    def test_password_is_read_hidden_and_never_printed(self):
        with (
            patch("pam_cli.auth.getpass.getpass", return_value="s3cret") as getpass,
            patch("builtins.input") as visible_input,
            patch("builtins.print") as print_,
        ):
            self.assertEqual(resolve_password(None), "s3cret")
        getpass.assert_called_once()  # typed without echo
        visible_input.assert_not_called()
        printed = " ".join(str(c.args) for c in print_.call_args_list)
        self.assertNotIn("s3cret", printed)
        self.assertIn("PAM password: ******", printed)  # fixed-length mask


class BannerTests(unittest.TestCase):
    def test_accepts_banner_only_when_shown(self):
        for shown in (True, False):
            with self.subTest(shown=shown):
                page = MagicMock()
                accept = page.get_by_role.return_value
                accept.is_visible.return_value = shown
                dismiss_authorized_use_banner(page, timeout_ms=2000)
                # Waits for the banner OR the login form, not the banner alone.
                accept.or_.assert_called_once_with(page.get_by_label.return_value)
                self.assertEqual(accept.click.called, shown)

    def test_returns_quietly_when_nothing_appears(self):
        page = MagicMock()
        accept = page.get_by_role.return_value
        accept.or_.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("")
        dismiss_authorized_use_banner(page, timeout_ms=2000)
        accept.click.assert_not_called()

    def test_totp_prompt_repeats_until_not_empty(self):
        with (
            patch("builtins.input", side_effect=["", "  ", " 123456 "]),
            patch("builtins.print"),
        ):
            self.assertEqual(resolve_totp(), "123456")


class LoginDetectionTests(unittest.TestCase):
    def test_is_logged_in(self):
        # Leaving the login page isn't enough: it could be an error page.
        page = MagicMock(url="https://pam.example/error")
        page.get_by_role.return_value.is_visible.return_value = False
        self.assertFalse(is_logged_in(page))
        page.get_by_role.return_value.is_visible.return_value = True
        self.assertTrue(is_logged_in(page))
        page.get_by_role.assert_called_with("tab", name="Accounts", exact=True)
        page.get_by_role.return_value.is_visible.side_effect = Exception("boom")
        self.assertFalse(is_logged_in(page))

    def test_wait_for_totp_or_success(self):
        page = MagicMock(url=URL)
        page.get_by_role.return_value.is_visible.return_value = False
        page.get_by_text.return_value.first.is_visible.return_value = True
        self.assertEqual(wait_for_totp_or_success(page, 1000), "totp")
        page.get_by_text.return_value.first.is_visible.return_value = False
        with (
            patch("pam_cli.polling.time.sleep"),
            patch("pam_cli.polling.time.monotonic", side_effect=[0, 0.5, 2]),
            self.assertRaisesRegex(PlaywrightTimeoutError, "TOTP screen"),
        ):
            wait_for_totp_or_success(page, 1000)

    def test_wait_for_totp_or_success_edge_cases(self):
        # The Accounts tab shows, even though the URL is still the login page.
        page = MagicMock(url=URL)
        page.get_by_role.return_value.is_visible.return_value = True
        self.assertEqual(wait_for_totp_or_success(page, 1000), "success")
        # A failing TOTP-heading check means "not yet", not a crash.
        page = MagicMock(url=URL)
        page.get_by_role.return_value.is_visible.return_value = False
        page.get_by_text.return_value.first.is_visible.side_effect = Exception("x")
        with (
            patch("pam_cli.polling.time.sleep"),
            patch("pam_cli.polling.time.monotonic", side_effect=[0, 0.5, 2]),
            self.assertRaises(PlaywrightTimeoutError),
        ):
            wait_for_totp_or_success(page, 1000)

    def test_wait_for_login_success(self):
        page = MagicMock(url=URL)
        page.get_by_role.return_value.is_visible.return_value = True
        wait_for_login_success(page, 1000)
        page = MagicMock(url=URL)
        page.get_by_role.return_value.is_visible.return_value = False
        with (
            patch("pam_cli.polling.time.sleep"),
            patch("pam_cli.polling.time.monotonic", side_effect=[0, 0.5, 2]),
            self.assertRaisesRegex(PlaywrightTimeoutError, "successful login"),
        ):
            wait_for_login_success(page, 1000)


if __name__ == "__main__":
    unittest.main()
