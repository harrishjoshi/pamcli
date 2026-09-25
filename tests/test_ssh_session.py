"""pam_cli.ssh_session: hours/reason resolution and the Quick Launch form."""

import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.config import (
    DEFAULT_SESSION_HOURS,
    MAX_SESSION_HOURS,
    QUICK_LAUNCH_SCOPE,
)
from pam_cli.ssh_session import request_ssh_access, resolve_hours, resolve_reason


@patch("builtins.print")
class ResolveTests(unittest.TestCase):
    def test_given_values_skip_the_prompt(self, _print):
        with patch("builtins.input") as prompt:
            self.assertEqual(resolve_hours(4), 4)
            self.assertEqual(resolve_reason("patching"), "patching")
        prompt.assert_not_called()

    def test_hours_prompt(self, _print):
        for answers, expected in (
            ([""], DEFAULT_SESSION_HOURS),
            (["1"], 1),
            ([str(MAX_SESSION_HOURS)], MAX_SESSION_HOURS),
            (["abc", "0", str(MAX_SESSION_HOURS + 1), "-3", "6"], 6),
        ):
            with (
                self.subTest(answers=answers),
                patch("builtins.input", side_effect=answers),
            ):
                self.assertEqual(resolve_hours(None), expected)

    def test_reason_prompt(self, _print):
        # No default: blank answers are asked again.
        with patch("builtins.input", side_effect=["", "  ", "patching"]) as prompt:
            self.assertEqual(resolve_reason(None), "patching")
        self.assertEqual(prompt.call_count, 3)
        with patch("builtins.input", return_value=" Quick check "):
            self.assertEqual(resolve_reason(None), "Quick check")


class RequestSshAccessTests(unittest.TestCase):
    @staticmethod
    def _page(existing_request_text=None):
        page = MagicMock()
        notice = page.get_by_text.return_value.first
        notice.is_visible.return_value = existing_request_text is not None
        notice.inner_text.return_value = existing_request_text
        return page

    def test_fills_form_requests_and_closes_the_launcher_tab(self):
        page = self._page()
        launcher = page.context.expect_page.return_value.__enter__.return_value.value

        self.assertEqual(request_ssh_access(page, 4, "Incident 42", 5000), "requested")
        quick_launch = page.locator(QUICK_LAUNCH_SCOPE)
        quick_launch.get_by_label.return_value.fill.assert_any_call("4", timeout=5000)
        quick_launch.get_by_label.return_value.fill.assert_any_call(
            "Incident 42", timeout=5000
        )
        quick_launch.get_by_role.assert_called_once_with(
            "button", name="Start SSH Session", exact=True
        )
        # Closed straight away, prompt and all, so the SSH app isn't opened.
        launcher.close.assert_called_once_with()
        launcher.wait_for_load_state.assert_not_called()
        # Only "requested" once the dialog has closed.
        quick_launch.wait_for.assert_called_once_with(state="hidden", timeout=5000)
        page.get_by_role.assert_not_called()  # not cancelled

    def test_no_launcher_tab_is_fine(self):
        page = self._page()
        waiting = page.context.expect_page.return_value
        waiting.__exit__.side_effect = PlaywrightTimeoutError("no popup")
        self.assertEqual(request_ssh_access(page, 4, "x", 5000), "requested")
        waiting.__enter__.return_value.value.close.assert_not_called()

    def test_a_failed_click_is_an_error_not_a_request(self):
        # The click timing out must not pass for "no launcher tab opened".
        page = self._page()
        page.locator.return_value.get_by_role.return_value.click.side_effect = (
            PlaywrightTimeoutError("button disabled")
        )
        with self.assertRaisesRegex(RuntimeError, "nothing was requested"):
            request_ssh_access(page, 4, "x", 5000)

    def test_a_dialog_that_stays_open_is_cancelled_and_not_confirmed(self):
        page = self._page()
        page.locator.return_value.wait_for.side_effect = PlaywrightTimeoutError("open")
        with self.assertLogs("pam_cli", "WARNING"):
            outcome = request_ssh_access(page, 4, "x", 5000)
        self.assertEqual(outcome, "not confirmed (the Access dialog stayed open)")
        page.get_by_role.assert_called_once_with("button", name="Cancel", exact=True)
        page.get_by_role.return_value.click.assert_called_once_with(timeout=5000)

    def test_existing_request_is_reused_and_the_dialog_cancelled(self):
        for notice, outcome in (
            (
                "Active request found. It is valid until 17:00 today.",
                "already requested (valid until 17:00 today)",
            ),
            (
                "Active request found.",
                "already requested",
            ),
        ):
            with self.subTest(outcome):
                page = self._page(notice)
                self.assertEqual(request_ssh_access(page, 4, "x", 5000), outcome)
                page.get_by_role.assert_called_once_with(
                    "button", name="Cancel", exact=True
                )
                quick_launch = page.locator.return_value
                quick_launch.get_by_label.return_value.fill.assert_not_called()
                page.context.expect_page.assert_not_called()  # nothing requested


if __name__ == "__main__":
    unittest.main()
