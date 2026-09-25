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
    QUICK_LAUNCH_SCOPE,
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

    Requesting means filling in hours and reason and clicking Start SSH
    Session. The portal then opens a tab that asks to launch the system's
    SSH app; pamcli closes it without launching anything."""
    quick_launch = page.locator(QUICK_LAUNCH_SCOPE)
    hours_field = quick_launch.get_by_label("Hours", exact=True)
    existing = page.get_by_text(EXISTING_REQUEST_TEXT).first
    hours_field.or_(existing).first.wait_for(state="visible", timeout=timeout_ms)
    if existing.is_visible():
        valid_until = re.search(r"valid until ([^.]+)", existing.inner_text())
        _cancel_dialog(page, timeout_ms)
        logger.debug("The account already has an active request")
        if valid_until:
            return f"already requested (valid until {valid_until.group(1)})"
        return "already requested"

    logger.debug("Filling in the session hours and reason")
    hours_field.fill(str(hours_value), timeout=timeout_ms)
    quick_launch.get_by_label("Reason", exact=True).fill(
        reason_value, timeout=timeout_ms
    )

    logger.debug("Requesting the access")
    start = quick_launch.get_by_role("button", name="Start SSH Session", exact=True)
    try:
        with page.context.expect_page(timeout=timeout_ms) as launcher:
            try:
                start.click(timeout=timeout_ms)
            except PlaywrightTimeoutError as exc:
                # Not the launcher tab timing out: the request never went out.
                raise RuntimeError(
                    "Couldn't click Start SSH Session; nothing was requested."
                ) from exc
    except PlaywrightTimeoutError:
        logger.debug("No SSH app launcher tab opened")
    else:
        launcher.value.close()
        logger.debug("Closed the SSH app launcher tab")

    # The dialog closes once the portal has taken the request; if it stays
    # open (e.g. showing an error), cancel it so the next IP can go ahead.
    try:
        quick_launch.wait_for(state="hidden", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.warning("The Access dialog stayed open after Start SSH Session")
        _cancel_dialog(page, timeout_ms)
        return "not confirmed (the Access dialog stayed open)"
    return "requested"
