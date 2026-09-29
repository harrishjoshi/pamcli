"""The Access dialog: setting session hours, reason and access types, and
requesting the access."""

import logging
import re
import sys

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.config import (
    DEFAULT_ACCESS,
    DEFAULT_SESSION_HOURS,
    EXISTING_REQUEST_TEXT,
    MAX_SESSION_HOURS,
    PASSKEY_SWITCH_NAME,
    REQUEST_SCOPE,
    REQUEST_TAB_NAME,
    SSH_SWITCH_NAME,
)

logger = logging.getLogger(__name__)

# What each access type requests, as shown in the results.
_REQUESTED = {"ssh": "SSH", "pass": "passkey", "both": "passkey and SSH"}


def resolve_hours(cli_value: int | None) -> int:
    """Return the given hours, else ask until a valid number is entered
    (Enter gives the default)."""
    if cli_value is not None:
        return cli_value
    while True:
        raw = input(
            f"Hours (default {DEFAULT_SESSION_HOURS}, max {MAX_SESSION_HOURS}): "
        ).strip()
        if not raw:
            hours = DEFAULT_SESSION_HOURS
        elif raw.isdecimal() and 1 <= int(raw) <= MAX_SESSION_HOURS:
            hours = int(raw)
        else:
            print(
                f"Enter a whole number between 1 and {MAX_SESSION_HOURS}.",
                file=sys.stderr,
            )
            continue
        print(f"Hours: {hours}", flush=True)
        return hours


def resolve_reason(cli_value: str | None) -> str:
    """Return the given reason, else ask until one is typed. There's no
    default, so every access request carries a real reason."""
    if cli_value is not None:
        return cli_value
    while not (reason := input("Reason: ").strip()):
        print("A reason is required.", file=sys.stderr)
    return reason


def _cancel_dialog(page: Page, timeout_ms: int) -> None:
    page.get_by_role("button", name="Cancel", exact=True).click(timeout=timeout_ms)


def _switch(form: Locator, label: str) -> Locator:
    return form.get_by_role("switch", name=label, exact=True)


class _NotOffered(RuntimeError):
    """The account doesn't offer this access type: its switch isn't there."""

    def __init__(self, what: str) -> None:
        super().__init__(f"The Access dialog has no {what} switch")
        self.what = what


def _is_on(switch: Locator, what: str, timeout_ms: int) -> bool:
    """Whether the switch is on. Raises _NotOffered if it isn't there, and
    an error if its state can't be read: an unknown state is never taken as
    "off"."""
    try:
        checked = switch.get_attribute("aria-checked", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise _NotOffered(what) from exc
    if checked not in ("true", "false"):
        raise RuntimeError(
            f"The Access dialog's {what} switch didn't say whether it is on; "
            "nothing was requested."
        )
    return checked == "true"


def _set_switch(
    form: Locator, label: str, what: str, on: bool, timeout_ms: int
) -> None:
    """Turn the switch with this label on or off; `what` names it in messages.

    A switch the account doesn't offer is skipped if it should be off, and
    raises _NotOffered if it should be on."""
    switch = _switch(form, label)
    # The switches are drawn by now, so a missing one isn't waited for.
    if switch.count() == 0:
        if on:
            raise _NotOffered(what)
        logger.debug("No %s switch in the Access dialog; leaving it", what)
        return
    if _is_on(switch, what, timeout_ms) != on:
        switch.click(timeout=timeout_ms)


def _check_switch(
    form: Locator, label: str, what: str, on: bool, timeout_ms: int
) -> None:
    """Raise unless the switch is as asked, so a click that didn't take
    can't send a request for more (or other) access than asked for."""
    switch = _switch(form, label)
    if switch.count() == 0:
        if on:
            raise _NotOffered(what)
        return
    if _is_on(switch, what, timeout_ms) != on:
        raise RuntimeError(
            f"The Access dialog's {what} switch isn't set as asked; "
            "nothing was requested."
        )


def request_access_in_dialog(
    page: Page,
    hours_value: int,
    reason_value: str,
    timeout_ms: int,
    access: str = DEFAULT_ACCESS,
) -> str:
    """Request `access` ("ssh", "pass" or "both") through the Submit Request
    tab, which never starts a session. Returns "requested (…)", "already
    requested (…)" (the existing request is kept; the dialog is cancelled),
    "skipped (…)" if the account doesn't offer that access (the dialog is
    cancelled) or "not confirmed (…)" if the dialog didn't close."""
    ssh_on = access in ("ssh", "both")
    passkey_on = access in ("pass", "both")
    existing = page.get_by_text(EXISTING_REQUEST_TEXT).first
    # The dialog is ready once it shows an Hours field or, if the account
    # already has an active request, the notice saying so.
    hours = page.get_by_role("dialog").get_by_label("Hours", exact=True)
    hours.or_(existing).first.wait_for(state="visible", timeout=timeout_ms)
    if existing.is_visible():
        valid_until = re.search(r"valid until ([^.]+)", existing.inner_text())
        _cancel_dialog(page, timeout_ms)
        logger.debug("The account already has an active request")
        if valid_until:
            return f"already requested (valid until {valid_until.group(1)})"
        return "already requested"

    page.get_by_role("tab", name=REQUEST_TAB_NAME, exact=True).click(timeout=timeout_ms)
    form = page.locator(REQUEST_SCOPE)
    logger.debug("Filling in the session hours and reason")
    form.get_by_label("Hours", exact=True).fill(str(hours_value), timeout=timeout_ms)
    form.get_by_label("Reason", exact=True).fill(reason_value, timeout=timeout_ms)
    # Wait for the switches to be drawn, so a missing one really is missing.
    try:
        form.get_by_role("switch").first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "The Access dialog shows no access switches; nothing was requested."
        ) from exc
    # The SSH switch is normally on and the passkey switch off already. The
    # passkey switch goes first, so turning SSH off never leaves both off.
    switches = (
        (PASSKEY_SWITCH_NAME, "passkey", passkey_on),
        (SSH_SWITCH_NAME, "SSH", ssh_on),
    )
    try:
        for label, what, on in switches:
            _set_switch(form, label, what, on, timeout_ms)
        for label, what, on in switches:
            _check_switch(form, label, what, on, timeout_ms)
    except _NotOffered as missing:
        # Nothing was submitted: cancel, and make sure the dialog closed so
        # the next IP can go ahead.
        logger.warning("The account offers no %s access; skipping it", missing.what)
        _cancel_dialog(page, timeout_ms)
        try:
            form.wait_for(state="hidden", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "The Access dialog didn't close after Cancel; stopping."
            ) from exc
        return f"skipped (no {missing.what} access for this account)"

    logger.debug("Requesting the access")
    submit = form.get_by_role("button", name=REQUEST_TAB_NAME, exact=True)
    try:
        submit.click(timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"Couldn't click {REQUEST_TAB_NAME}; nothing was requested."
        ) from exc

    # The dialog closes once the portal has taken the request; if it stays
    # open (e.g. showing an error), cancel it so the next IP can go ahead.
    try:
        form.wait_for(state="hidden", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.warning("The Access dialog stayed open after %s", REQUEST_TAB_NAME)
        _cancel_dialog(page, timeout_ms)
        return "not confirmed (the Access dialog stayed open)"
    return f"requested ({_REQUESTED[access]})"
