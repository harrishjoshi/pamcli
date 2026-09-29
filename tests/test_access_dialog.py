"""pam_cli.access_dialog: hours/reason resolution and the Access dialog."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.access_dialog import (
    request_access_in_dialog,
    resolve_hours,
    resolve_reason,
)
from pam_cli.config import (
    DEFAULT_SESSION_HOURS,
    MAX_SESSION_HOURS,
    PASSKEY_SWITCH_NAME,
    REQUEST_SCOPE,
    REQUEST_TAB_NAME,
    SSH_SWITCH_NAME,
)


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


class RequestAccessInDialogTests(unittest.TestCase):
    @staticmethod
    def _dialog(
        existing_request_text=None,
        ssh_already_on=True,
        passkey_on=False,
        missing=(),
        unreadable=(),
        stuck=(),
    ):
        """A fake Access dialog, with the parts the tests look at by name.

        existing_request_text: the existing-request notice, if the account
        already has an active request.
        ssh_already_on: whether the dialog's SSH switch is already on when
        it opens (the portal's "SSH Session" switch, on already).
        passkey_on: whether its passkey switch is already on (it's off on
        the real portal).
        missing: labels of switches the dialog doesn't show, as when the
        account's access policy leaves them out.
        unreadable: labels of switches the dialog shows but whose state
        can't be read, as when the portal marks them differently.
        stuck: labels of switches that a click doesn't change."""
        ui = SimpleNamespace(
            page=MagicMock(),
            dialog=MagicMock(),  # the dialog itself, to tell when it's ready
            tab_or_cancel=MagicMock(),  # the Submit Request tab or Cancel
            form=MagicMock(),  # the Submit Request tab's form
            hours=MagicMock(),
            reason=MagicMock(),
            ssh_switch=MagicMock(),
            passkey_switch=MagicMock(),
            any_switch=MagicMock(),  # the first switch, to tell they're drawn
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
        switches = {
            SSH_SWITCH_NAME: ui.ssh_switch,
            PASSKEY_SWITCH_NAME: ui.passkey_switch,
        }
        state = {SSH_SWITCH_NAME: ssh_already_on, PASSKEY_SWITCH_NAME: passkey_on}
        for name, switch in switches.items():
            switch.count.return_value = 0 if name in missing else 1
            if name in missing:
                switch.get_attribute.side_effect = PlaywrightTimeoutError("none")
            elif name in unreadable:
                # Present, but no aria-checked: get_attribute returns None.
                switch.get_attribute.return_value = None
            else:
                switch.get_attribute.side_effect = lambda *_, name=name, **__: (
                    "true" if state[name] else "false"
                )
            if name not in stuck:
                switch.click.side_effect = lambda *_, name=name, **__: state.update(
                    {name: not state[name]}
                )

        def get_by_role(role, name=None, **_):
            if role != "switch":
                return ui.submit
            return switches[name] if name else ui.any_switch

        ui.form.get_by_role.side_effect = get_by_role
        return ui

    def test_requests_through_the_submit_request_tab(self):
        ui = self._dialog()
        self.assertEqual(
            request_access_in_dialog(ui.page, 4, "Incident 42", 5000), "requested (SSH)"
        )
        # Waits for the dialog to show an Hours field before doing anything.
        ui.dialog.get_by_label.assert_called_once_with("Hours", exact=True)
        ui.page.get_by_role.assert_any_call("tab", name=REQUEST_TAB_NAME, exact=True)
        ui.tab_or_cancel.click.assert_called_once_with(timeout=5000)
        ui.hours.fill.assert_called_once_with("4", timeout=5000)
        ui.reason.fill.assert_called_once_with("Incident 42", timeout=5000)
        ui.ssh_switch.click.assert_not_called()  # already on
        ui.passkey_switch.click.assert_not_called()  # stays off
        ui.form.get_by_role.assert_any_call("button", name=REQUEST_TAB_NAME, exact=True)
        ui.submit.click.assert_called_once_with(timeout=5000)
        # Only "requested (…)" once the dialog has closed.
        ui.form.wait_for.assert_called_once_with(state="hidden", timeout=5000)

    def test_turns_the_ssh_switch_on_if_it_is_off(self):
        ui = self._dialog(ssh_already_on=False)
        request_access_in_dialog(ui.page, 4, "x", 5000)
        ui.ssh_switch.click.assert_called_once_with(timeout=5000)

    def test_both_requests_pass_along_with_ssh(self):
        ui = self._dialog()
        self.assertEqual(
            request_access_in_dialog(ui.page, 4, "x", 5000, access="both"),
            "requested (passkey and SSH)",
        )
        ui.passkey_switch.click.assert_called_once_with(timeout=5000)
        ui.ssh_switch.click.assert_not_called()  # stays on

    def test_pass_only_turns_ssh_off_after_the_passkey_switch_is_on(self):
        ui = self._dialog()
        order = MagicMock()
        order.attach_mock(ui.passkey_switch.click, "pass")
        order.attach_mock(ui.ssh_switch.click, "ssh")
        self.assertEqual(
            request_access_in_dialog(ui.page, 4, "x", 5000, access="pass"),
            "requested (passkey)",
        )
        # Never both off, even for a moment.
        self.assertEqual([name for name, *_ in order.mock_calls], ["pass", "ssh"])

    def test_passkey_switch_is_turned_off_unless_pass_is_asked_for(self):
        ui = self._dialog(passkey_on=True)
        self.assertEqual(
            request_access_in_dialog(ui.page, 4, "x", 5000), "requested (SSH)"
        )
        ui.passkey_switch.click.assert_called_once_with(timeout=5000)

    def test_a_missing_switch_that_should_be_off_is_skipped(self):
        for access, missing in (
            ("ssh", PASSKEY_SWITCH_NAME),
            ("pass", SSH_SWITCH_NAME),
        ):
            with self.subTest(access=access):
                ui = self._dialog(missing=(missing,))
                request_access_in_dialog(ui.page, 4, "x", 5000, access=access)
                ui.submit.click.assert_called_once_with(timeout=5000)

    def test_a_missing_switch_that_should_be_on_skips_the_ip(self):
        for access, missing, what in (
            ("pass", PASSKEY_SWITCH_NAME, "passkey"),
            ("both", PASSKEY_SWITCH_NAME, "passkey"),
            ("ssh", SSH_SWITCH_NAME, "SSH"),
        ):
            with self.subTest(access=access, missing=missing):
                ui = self._dialog(missing=(missing,))
                with self.assertLogs("pam_cli", "WARNING"):
                    outcome = request_access_in_dialog(
                        ui.page, 4, "x", 5000, access=access
                    )
                self.assertEqual(
                    outcome, f"skipped (no {what} access for this account)"
                )
                ui.submit.click.assert_not_called()
                # Skipped at once, without waiting for the missing switch.
                switch = ui.passkey_switch if what == "passkey" else ui.ssh_switch
                switch.get_attribute.assert_not_called()
                # Cancelled and closed, so the next IP can go ahead.
                ui.page.get_by_role.assert_called_with(
                    "button", name="Cancel", exact=True
                )
                ui.form.wait_for.assert_called_once_with(state="hidden", timeout=5000)

    def test_a_skip_whose_dialog_stays_open_stops_the_run(self):
        ui = self._dialog(missing=(PASSKEY_SWITCH_NAME,))
        ui.form.wait_for.side_effect = PlaywrightTimeoutError("open")
        with (
            self.assertLogs("pam_cli", "WARNING"),
            self.assertRaisesRegex(RuntimeError, "didn't close after Cancel"),
        ):
            request_access_in_dialog(ui.page, 4, "x", 5000, access="pass")
        ui.submit.click.assert_not_called()

    def test_an_unreadable_switch_state_is_an_error_not_a_request(self):
        # Treating "state unknown" as "off" would click a switch on that had
        # to stay off, so every unreadable state stops the run instead.
        for access, unreadable, what in (
            ("pass", SSH_SWITCH_NAME, "SSH"),  # must be off
            ("ssh", SSH_SWITCH_NAME, "SSH"),  # must be on
            ("pass", PASSKEY_SWITCH_NAME, "passkey"),  # must be on
        ):
            with self.subTest(access=access, unreadable=unreadable):
                ui = self._dialog(unreadable=(unreadable,))
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"{what} switch didn't say whether it is on.*nothing was requested",
                ):
                    request_access_in_dialog(ui.page, 4, "x", 5000, access=access)
                ui.submit.click.assert_not_called()

    def test_waits_for_the_switches_before_checking_them(self):
        ui = self._dialog()
        request_access_in_dialog(ui.page, 4, "x", 5000)
        ui.any_switch.first.wait_for.assert_called_once_with(
            state="visible", timeout=5000
        )

    def test_a_dialog_without_switches_is_an_error_not_a_request(self):
        ui = self._dialog()
        ui.any_switch.first.wait_for.side_effect = PlaywrightTimeoutError("none")
        with self.assertRaisesRegex(RuntimeError, "no access switches"):
            request_access_in_dialog(ui.page, 4, "x", 5000)
        ui.submit.click.assert_not_called()

    def test_a_click_that_did_not_take_is_an_error_not_a_request(self):
        # e.g. the passkey switch stays on for an SSH-only request.
        for access, stuck, what, kwargs in (
            ("ssh", PASSKEY_SWITCH_NAME, "passkey", {"passkey_on": True}),
            ("pass", SSH_SWITCH_NAME, "SSH", {}),
            ("both", PASSKEY_SWITCH_NAME, "passkey", {}),
        ):
            with self.subTest(access=access, stuck=stuck):
                ui = self._dialog(stuck=(stuck,), **kwargs)
                with self.assertRaisesRegex(
                    RuntimeError, f"{what} switch isn't set as asked"
                ):
                    request_access_in_dialog(ui.page, 4, "x", 5000, access=access)
                ui.submit.click.assert_not_called()

    def test_a_failed_click_is_an_error_not_a_request(self):
        ui = self._dialog()
        ui.submit.click.side_effect = PlaywrightTimeoutError("button disabled")
        with self.assertRaisesRegex(RuntimeError, "nothing was requested"):
            request_access_in_dialog(ui.page, 4, "x", 5000)

    def test_a_dialog_that_stays_open_is_cancelled_and_not_confirmed(self):
        ui = self._dialog()
        ui.form.wait_for.side_effect = PlaywrightTimeoutError("open")
        with self.assertLogs("pam_cli", "WARNING"):
            outcome = request_access_in_dialog(ui.page, 4, "x", 5000)
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
                self.assertEqual(
                    request_access_in_dialog(ui.page, 4, "x", 5000), outcome
                )
                ui.page.get_by_role.assert_called_with(
                    "button", name="Cancel", exact=True
                )
                ui.hours.fill.assert_not_called()
                ui.submit.click.assert_not_called()  # nothing requested


if __name__ == "__main__":
    unittest.main()
