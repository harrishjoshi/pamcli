"""The Access dialog: setting session hours and reason, and requesting the
access."""

import logging
import re
import sys

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.config import (
    DEFAULT_SESSION_HOURS,
    EXISTING_REQUEST_TEXT,
    MAX_SESSION_HOURS,
    REQUEST_SCOPE,
    REQUEST_TAB_NAME,
)

logger = logging.getLogger(__name__)


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


def request_ssh_access(
    page: Page, hours_value: int, reason_value: str, timeout_ms: int
) -> str:
    """Request access in the Access dialog and return what happened, for the
    summary: "requested"; "already requested (…)" if the account already
    has an active request, in which case the dialog is just cancelled; or
    "not confirmed (…)" if the dialog didn't close afterwards.

    The request goes through the dialog's Submit Request tab, which only
    requests SSH access and never starts the session."""
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
    # The request is for SSH access; the switch is normally on already.
    ssh = form.get_by_role("switch", name="SSH Session", exact=True)
    if ssh.get_attribute("aria-checked", timeout=timeout_ms) != "true":
        ssh.click(timeout=timeout_ms)

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
    return "requested"
