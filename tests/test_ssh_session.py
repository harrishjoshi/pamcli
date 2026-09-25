"""pam_cli.ssh_session: hours/reason resolution and the Access dialog."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.config import (
    DEFAULT_SESSION_HOURS,
    MAX_SESSION_HOURS,
    REQUEST_SCOPE,
    REQUEST_TAB_NAME,
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
    def _dialog(existing_request_text=None, ssh_session_already_on=True):
        """A fake Access dialog, with the parts the tests look at by name.

        existing_request_text: the existing-request notice, if the account
        already has an active request.
        ssh_session_already_on: whether the Submit Request tab's SSH Session
        switch is already on when the tab opens (it is on the real portal)."""
        ui = SimpleNamespace(
            page=MagicMock(),
            dialog=MagicMock(),  # the dialog itself, to tell when it's ready
            tab_or_cancel=MagicMock(),  # the Submit Request tab or Cancel
            form=MagicMock(),  # the Submit Request tab's form
            hours=MagicMock(),
            reason=MagicMock(),
            ssh_session_switch=MagicMock(),
            submit=MagicMock(),
        )
        notice = ui.page.get_by_text.return_value.first
        notice.is_visible.return_value = existing_request_text is not None
        notice.inner_text.return_value = existing_request_text
        ui.page.get_by_role.side_effect = lambda role, **_: (
            ui.dialog if role == "dialog" else ui.tab_or_cancel
        )
        ui.page.locator.side_effect = {REQUEST_SCOPE: ui.form}.__getitem__
        ui.form.get_by_label.side_effect = lambda label, **_: {
            "Hours": ui.hours,
            "Reason": ui.reason,
        }[label]
        ui.ssh_session_switch.get_attribute.return_value = (
            "true" if ssh_session_already_on else "false"
        )
        ui.form.get_by_role.side_effect = lambda role, **_: (
            ui.ssh_session_switch if role == "switch" else ui.submit
        )
        return ui

    def test_requests_through_the_submit_request_tab(self):
        ui = self._dialog()
        self.assertEqual(
            request_ssh_access(ui.page, 4, "Incident 42", 5000), "requested"
        )
        # Waits for the dialog to show an Hours field before doing anything.
        ui.dialog.get_by_label.assert_called_once_with("Hours", exact=True)
        ui.page.get_by_role.assert_any_call("tab", name=REQUEST_TAB_NAME, exact=True)
        ui.tab_or_cancel.click.assert_called_once_with(timeout=5000)
        ui.hours.fill.assert_called_once_with("4", timeout=5000)
        ui.reason.fill.assert_called_once_with("Incident 42", timeout=5000)
        ui.ssh_session_switch.click.assert_not_called()  # already on
        ui.form.get_by_role.assert_any_call("button", name=REQUEST_TAB_NAME, exact=True)
        ui.submit.click.assert_called_once_with(timeout=5000)
        # Only "requested" once the dialog has closed.
        ui.form.wait_for.assert_called_once_with(state="hidden", timeout=5000)

    def test_turns_the_ssh_session_switch_on_if_it_is_off(self):
        ui = self._dialog(ssh_session_already_on=False)
        request_ssh_access(ui.page, 4, "x", 5000)
        ui.ssh_session_switch.click.assert_called_once_with(timeout=5000)

    def test_a_failed_click_is_an_error_not_a_request(self):
        ui = self._dialog()
        ui.submit.click.side_effect = PlaywrightTimeoutError("button disabled")
        with self.assertRaisesRegex(RuntimeError, "nothing was requested"):
            request_ssh_access(ui.page, 4, "x", 5000)

    def test_a_dialog_that_stays_open_is_cancelled_and_not_confirmed(self):
        ui = self._dialog()
        ui.form.wait_for.side_effect = PlaywrightTimeoutError("open")
        with self.assertLogs("pam_cli", "WARNING"):
            outcome = request_ssh_access(ui.page, 4, "x", 5000)
        self.assertEqual(outcome, "not confirmed (the Access dialog stayed open)")
        ui.page.get_by_role.assert_called_with("button", name="Cancel", exact=True)

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
                ui = self._dialog(notice)
                self.assertEqual(request_ssh_access(ui.page, 4, "x", 5000), outcome)
                ui.page.get_by_role.assert_called_with(
                    "button", name="Cancel", exact=True
                )
                ui.hours.fill.assert_not_called()
                ui.submit.click.assert_not_called()  # nothing requested


if __name__ == "__main__":
    unittest.main()
