"""Logging in: the notice, username and password, then the TOTP code."""

import getpass
import logging
import os
import sys
import urllib.parse
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.blocker import install_input_blocker, remove_input_blocker
from pam_cli.config import TEXT, TOTP_INPUT_SELECTOR
from pam_cli.paths import write_private
from pam_cli.polling import poll_until

logger = logging.getLogger(__name__)

FAILURE_SCREENSHOT_PATH = "login_failed.png"
# Always the same length, so the password's length isn't shown.
_PASSWORD_MASK = "******"
TOTP_ATTEMPTS = 3
# How long to wait after submitting a TOTP code before treating it as
# rejected, if the TOTP field is still showing.
_TOTP_REJECT_WINDOW_MS = 10_000


def dismiss_authorized_use_banner(page: Page, timeout_ms: int) -> None:
    """Accept the 'AUTHORIZED USE ONLY' notice if it appears.

    Waits for the notice or the login form, whichever comes first, so a
    portal without the notice doesn't slow the login down."""
    logger.debug("Accepting the authorized-use banner if it's shown")
    accept = page.get_by_role("button", name=TEXT["accept_button"], exact=True)
    username = page.get_by_label(TEXT["username_label"], exact=True)
    try:
        accept.or_(username).first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return  # neither appeared; the caller reports the missing form
    if accept.is_visible():
        accept.click(timeout=timeout_ms)


def resolve_url() -> str:
    """Return the portal's login URL from PAM_URL, after checking it's safe."""
    url = os.environ.get("PAM_URL", "").strip()
    if not url:
        raise ValueError(
            "PAM_URL is not set. Set it to the portal's login page, e.g. "
            "https://pam.example.com/login."
        )
    return check_url(url)


def check_url(url: str) -> str:
    """Return the URL if it's safe to use as PAM_URL, else raise ValueError."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        # Checked first: the URL is logged and shown in the errors below, so
        # it mustn't contain credentials.
        raise ValueError("PAM_URL must not contain a username or password.")
    if parsed.scheme != "https" or not parsed.hostname:
        # Anything else would send the password and TOTP code unencrypted.
        raise ValueError(f"PAM_URL must be an https:// URL, got {url!r}.")
    if "login" not in f"{parsed.path}#{parsed.fragment}".lower():
        # The host doesn't count: login.example.com/ isn't a login page.
        raise ValueError(
            f"PAM_URL must be the portal's login page (its path contains "
            f"'login'), got {url!r}."
        )
    return url


def take_env_password() -> str | None:
    """Return PAM_PASSWORD and remove it from the environment, so the
    browser processes started later never see it."""
    return os.environ.pop("PAM_PASSWORD", None) or None


def resolve_username() -> str:
    """PAM_USERNAME if set (shown, since there's no prompt), else ask."""
    username = os.environ.get("PAM_USERNAME", "").strip()
    if username:
        print(f"PAM username: {username}", flush=True)
        return username
    username = input("PAM username: ").strip()
    if not username:
        raise ValueError(
            "Username cannot be empty — set PAM_USERNAME or enter it at the prompt."
        )
    return username


def resolve_password(env_value: str | None) -> str:
    if env_value:
        print(f"PAM password: {_PASSWORD_MASK}", flush=True)
        return env_value
    password = getpass.getpass("PAM password: ")
    # getpass leaves the prompt line looking empty; rewrite it with a masked
    # value. The classic Windows console can't, so it just gets a new line.
    prefix = "" if sys.platform == "win32" else "\033[1A\033[2K"
    print(f"{prefix}PAM password: {_PASSWORD_MASK}", flush=True)
    return password


def resolve_totp() -> str:
    # Always asked for: a TOTP code works once, so it's never read from env.
    while not (code := input("TOTP code: ").strip()):
        print("The TOTP code can't be empty.", file=sys.stderr)
    return code


def is_logged_in(page: Page) -> bool:
    """True once the portal is showing its Accounts tab. The URL alone
    isn't trusted: an error or password-change page also leaves the login
    page."""
    try:
        return page.get_by_role("tab", name="Accounts", exact=True).is_visible()
    except Exception as exc:
        logger.debug("is_logged_in: Accounts tab check failed: %s", exc)
        return False


def wait_for_totp_or_success(page: Page, timeout_ms: int) -> str:
    """Wait for the TOTP screen ('totp') or the portal itself ('success')."""
    logger.debug("Waiting for the TOTP screen or the portal")

    def check() -> str | None:
        if is_logged_in(page):
            return "success"
        try:
            if page.get_by_text(TEXT["totp_heading"]).first.is_visible():
                return "totp"
        except Exception as exc:
            logger.debug("TOTP heading check failed: %s", exc)
        return None

    outcome = poll_until(check, timeout_ms)
    if outcome is None:
        raise PlaywrightTimeoutError(
            "Neither the TOTP screen nor a post-login redirect appeared after "
            f"submitting credentials. Last seen URL: {page.url}"
        )
    return outcome


def wait_for_login_success(page: Page, timeout_ms: int) -> None:
    logger.debug("Waiting for the portal")
    if poll_until(lambda: is_logged_in(page) or None, timeout_ms) is None:
        raise PlaywrightTimeoutError(
            f"Did not detect a successful login in time. Last seen URL: {page.url}"
        )


def _fail(page: Page, message: str) -> RuntimeError:
    """Return the error to raise, with a screenshot of the page if one can be
    saved. A crashed or closed page can't be captured, and that mustn't hide
    the real error."""
    try:
        write_private(Path(FAILURE_SCREENSHOT_PATH), page.screenshot())
    except (PlaywrightError, OSError) as exc:
        logger.debug("Couldn't save %s: %s", FAILURE_SCREENSHOT_PATH, exc)
        return RuntimeError(message)
    return RuntimeError(f"{message} See {FAILURE_SCREENSHOT_PATH}.")


def _check_site(page: Page, url: str, when: str) -> None:
    """Raise unless the page is still on PAM_URL's site over https.

    Checked before typing a credential, so a redirect to another site (or
    to plain http) doesn't receive it, and again after, so a redirect while
    typing stops the login before anything is submitted there."""
    expected, current = urllib.parse.urlsplit(url), urllib.parse.urlsplit(page.url)
    if (
        current.scheme != "https"
        or current.hostname != expected.hostname
        or (current.port or 443) != (expected.port or 443)
    ):
        raise RuntimeError(
            f"The login page moved to {page.url}, which isn't PAM_URL's site "
            f"({expected.hostname}) over https, {when}; nothing was submitted."
        )


def _type_and_verify(
    page: Page,
    url: str,
    field: Locator,
    value: str,
    name: str,
    timeout_ms: int,
    clear_first: bool = False,
) -> None:
    """Type a value into a field on PAM_URL's site and check that it arrived.

    The page only accepts real keystrokes, and only saves a field's value
    once the field loses focus, so the field is typed into and then left.
    Reading it back catches a hidden duplicate field being filled instead."""
    _check_site(page, url, f"before the {name} was typed")
    if clear_first:
        field.fill("", timeout=timeout_ms)  # clear a rejected code
    field.focus(timeout=timeout_ms)
    # Never fill() a credential: Playwright quotes fill()'s value in its
    # error messages, which are printed. press_sequentially's errors don't.
    field.press_sequentially(value, delay=50, timeout=timeout_ms)
    field.blur(timeout=timeout_ms)
    _check_site(page, url, f"while the {name} was being typed")
    if field.input_value() != value:
        raise _fail(
            page,
            f"The {name} field didn't accept what was typed; the login page "
            "may have changed (see `pamcli discover`).",
        )


def _click(page: Page, button_name: str, timeout_ms: int) -> None:
    # Press the button directly; a normal click would land on the layer that
    # blocks manual input.
    page.get_by_role("button", name=button_name, exact=True).dispatch_event(
        "click", timeout=timeout_ms
    )


def _totp_accepted(page: Page, totp_field: Locator, timeout_ms: int) -> bool:
    """After submitting a TOTP code, wait to see whether it worked.

    True once logged in; False if the TOTP field is still showing after a
    short wait, meaning the code was rejected."""
    window_ms = min(_TOTP_REJECT_WINDOW_MS, timeout_ms)
    if poll_until(lambda: is_logged_in(page) or None, window_ms):
        return True
    if totp_field.is_visible():
        return False
    wait_for_login_success(page, timeout_ms)
    return True


def do_login(
    page: Page, url: str, timeout_ms: int, env_password: str | None = None
) -> str:
    """Log in and return the URL the portal lands on. The password comes
    from PAM_PASSWORD if it was set; otherwise it's asked for."""
    logger.debug("Opening the login page")
    page.goto(url, timeout=timeout_ms)

    dismiss_authorized_use_banner(page, timeout_ms=timeout_ms)

    username_field = page.get_by_label(TEXT["username_label"], exact=True)
    try:
        username_field.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Could not find the login form. Check the VPN or network "
            "connection; if the login page has changed, run `pamcli discover`."
        ) from exc

    install_input_blocker(page)
    try:
        username = resolve_username()
        password = resolve_password(env_password)

        logger.debug("Filling in and submitting the login form")
        _type_and_verify(page, url, username_field, username, "Username", timeout_ms)
        password_field = page.get_by_label(TEXT["password_label"], exact=True)
        _type_and_verify(page, url, password_field, password, "Password", timeout_ms)
        _click(page, TEXT["login_button"], timeout_ms)

        try:
            outcome = wait_for_totp_or_success(page, timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise _fail(
                page,
                "Login didn't go through. Check the username and password; if "
                "they're right, the login page may have changed.",
            ) from exc

        if outcome == "totp":
            # Found by name: the hidden password field is still on the page and
            # could otherwise be filled by mistake.
            totp_field = page.locator(TOTP_INPUT_SELECTOR)
            for attempt in range(1, TOTP_ATTEMPTS + 1):
                totp_code = resolve_totp()
                logger.debug("Submitting the TOTP code")
                _type_and_verify(
                    page,
                    url,
                    totp_field,
                    totp_code,
                    "TOTP",
                    timeout_ms,
                    clear_first=True,
                )
                _click(page, TEXT["totp_submit_button"], timeout_ms)
                try:
                    if _totp_accepted(page, totp_field, timeout_ms):
                        break
                except PlaywrightTimeoutError as exc:
                    raise _fail(
                        page,
                        "The portal didn't open after the TOTP code was accepted.",
                    ) from exc
                if attempt < TOTP_ATTEMPTS:
                    print("That code didn't work — try again.", file=sys.stderr)
            else:
                raise _fail(page, f"The TOTP code was rejected {TOTP_ATTEMPTS} times.")
    finally:
        remove_input_blocker(page)

    print("Login successful.")
    return page.url
