"""`pamcli discover`: for when login stops working after a portal update."""

import html
import logging
import re
from pathlib import Path

from playwright.sync_api import ElementHandle, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.auth import dismiss_authorized_use_banner
from pam_cli.config import TEXT
from pam_cli.logging_setup import printable
from pam_cli.paths import write_private

logger = logging.getLogger(__name__)

FIELDS_PATH = "login_page_fields.txt"
SCREENSHOT_PATH = "login_page.png"
_ATTRIBUTES = ("name", "id", "type", "for", "placeholder")
# Ids like these change on every page load, so they're left out.
_RANDOM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _describe(tag: str, element: ElementHandle) -> str | None:
    """One element as a line of HTML with only its useful attributes, e.g.
    <button type="submit">Continue</button>. None if there's nothing to match
    on, like an icon-only button."""
    attributes = {}
    for name in _ATTRIBUTES:
        value = (element.get_attribute(name) or "").strip()
        if value and not _RANDOM_ID.fullmatch(value):
            attributes[name] = value
    text = " ".join((element.inner_text() or "").split())[:60]
    if not text and attributes.keys() <= {"type"}:
        return None
    opening = "".join(
        f' {name}="{html.escape(value)}"' for name, value in attributes.items()
    )
    if tag == "input":  # an input has no text or closing tag
        return f"<input{opening}>"
    return f"<{tag}{opening}>{html.escape(text)}</{tag}>"


def discover(page: Page, url: str, timeout_ms: int) -> None:
    """Write the login page's fields to a text file and save a screenshot."""
    logger.debug("Opening the login page")
    page.goto(url, timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        print("(The page is still loading; continuing anyway.)")

    dismiss_authorized_use_banner(page, timeout_ms=timeout_ms)

    # The form may only mount after the banner is accepted.
    try:
        page.get_by_label(TEXT["username_label"], exact=True).wait_for(
            state="visible", timeout=timeout_ms
        )
    except PlaywrightTimeoutError:
        print("(The login form didn't appear; listing what's on the page anyway.)")

    fields = [
        line
        for tag in ("input", "button", "label")
        for element in page.query_selector_all(tag)
        if (line := _describe(tag, element))
    ]
    # Absolute for the messages below, but not resolve()d: that would follow
    # a symlink planted at this name and get past write_private's refusal.
    fields_path = Path.cwd() / FIELDS_PATH
    report = "\n".join([f"Login page fields at {page.url}", "", *fields, ""])
    # The report is meant to be read in a terminal (e.g. with cat), and quotes
    # the page's own text.
    report = printable(report)
    write_private(fields_path, report.encode("utf-8"))

    screenshot = Path.cwd() / SCREENSHOT_PATH
    write_private(screenshot, page.screenshot(full_page=True))

    print(f"Wrote {len(fields)} fields to {fields_path}")
    print(f"Screenshot saved to {screenshot}")
    print("Compare them with TEXT and the selectors in src/pam_cli/config.py.")
