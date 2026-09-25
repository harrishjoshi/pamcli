"""Finding servers in the accounts grid and requesting access to them."""

import logging
import re
import sys
from typing import TypeVar

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from pam_cli.config import (
    ACCESS_CELL_SELECTOR,
    ACCOUNT_NAME_CELL_SELECTOR,
    GRID_ROW_SELECTOR,
)
from pam_cli.environments import EnvironmentConfig, IPTarget, parse_ip_list
from pam_cli.logging_setup import printable
from pam_cli.polling import poll_until
from pam_cli.ssh_session import request_ssh_access, resolve_hours, resolve_reason

logger = logging.getLogger(__name__)
T = TypeVar("T")

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d])")


def open_directory_linked_accounts(page: Page, timeout_ms: int) -> None:
    """Open the portal's Accounts tab, then Directory Linked Accounts."""
    logger.debug("Opening Accounts > Directory Linked Accounts")
    page.get_by_role("tab", name="Accounts", exact=True).click(timeout=timeout_ms)
    # Match the button, not the text: the grid's heading uses the same words.
    page.get_by_role("button", name="Directory Linked Accounts", exact=True).click(
        timeout=timeout_ms
    )


def show_requests(page: Page, timeout_ms: int) -> None:
    """Show the portal's Requests tab, then its Approved list. Best-effort:
    the access was already requested, so a problem here is only a warning."""
    # Found by name as a tab, button or link, whichever the portal uses.
    approved = (
        page.get_by_role("tab", name="Approved", exact=True)
        .or_(page.get_by_role("button", name="Approved", exact=True))
        .or_(page.get_by_role("link", name="Approved", exact=True))
    )
    try:
        page.get_by_role("tab", name="Requests", exact=True).click(timeout=timeout_ms)
        approved.first.click(timeout=timeout_ms)
    except PlaywrightError as exc:
        logger.warning("Couldn't open Requests > Approved: %s", exc)


def _close_tab(tab: Page) -> None:
    """Close a tab the portal opened while requesting, e.g. the one offering
    to start the SSH session: pamcli only requests access, never starts it."""
    # Its address isn't logged: a session-launch link can carry a token.
    logger.debug("Closing a tab the portal opened")
    tab.close()


def type_quick_filter(page: Page, filter_text: str, timeout_ms: int) -> None:
    logger.debug("Typing %s into the Quick filter", filter_text)
    quick_filter = page.get_by_placeholder("Quick filter", exact=True)
    quick_filter.click(timeout=timeout_ms)
    quick_filter.fill("", timeout=timeout_ms)  # clear the previous IP
    quick_filter.press_sequentially(filter_text, delay=30)


def _shows_filter_result(texts: list[str], ip: str) -> bool:
    """True if the rows can be the Quick filter's result for this IP: every
    row contains it. Rows left over from the previous IP don't. A grid that
    shows no IPs can't be checked, so it passes (and is rejected later)."""
    return all(ip in text for text in texts) or not any(
        _IPV4.search(text) for text in texts
    )


def wait_for_grid_settled(page: Page, ip: str, timeout_ms: int) -> int:
    """Wait for the grid to finish filtering to this IP and return its row
    count.

    The filter can lag behind typing, so the rows have to be the filter's
    result for this IP, with the same count on two checks in a row, before
    they're trusted."""
    rows = page.locator(GRID_ROW_SELECTOR)
    previous: list[int] = []

    def check() -> int | None:
        count = rows.count()
        if count == 0:
            try:
                if not page.get_by_text(
                    "There are no records to display.", exact=True
                ).is_visible():
                    count = -1  # still loading
            except Exception as exc:
                logger.debug("No-records check failed: %s", exc)
                count = -1
        elif not _shows_filter_result(rows.all_inner_texts(), ip):
            count = -1  # still showing the previous IP's rows
        settled = count >= 0 and previous == [count]
        previous[:] = [count]
        return count if settled else None

    settled = poll_until(check, timeout_ms, poll_interval=0.3)
    if settled is None:
        raise RuntimeError(f"The accounts grid didn't finish filtering to {ip}.")
    return settled


def _exact_ip(ip: str) -> re.Pattern[str]:
    """Matches the IP in text, but not as part of a longer one."""
    return re.compile(rf"(?<![\d.]){re.escape(ip)}(?!\d)")


def _rows_for_ip(rows: list[Locator], ip: str, timeout_ms: int) -> list[Locator]:
    """Keep only the rows that show exactly this IP.

    The Quick filter matches partial text, so 10.0.0.1 also brings up
    10.0.0.15. If the grid shows no IPs at all, the rows can't be checked,
    so nothing is requested rather than guessing."""
    texts = [row.inner_text(timeout=timeout_ms) for row in rows]
    if texts and not any(_IPV4.search(text) for text in texts):
        raise RuntimeError(
            f"The accounts grid shows no IP addresses, so its rows can't be "
            f"confirmed to belong to {ip}; nothing was requested for it."
        )
    exact = _exact_ip(ip)
    return [row for row, text in zip(rows, texts) if exact.search(text)]


def _account_name(row: Locator, timeout_ms: int) -> str:
    return (
        row.locator(ACCOUNT_NAME_CELL_SELECTOR).inner_text(timeout=timeout_ms).strip()
    )


def _matching_accounts(names: list[str], account: str) -> list[str]:
    """The account names that match: the exact name (ignoring case) if it's
    there, else every name containing it (e.g. 'app01' in 'svc.app01')."""
    wanted = account.strip().lower()
    exact = [name for name in names if name.lower() == wanted]
    return exact or [name for name in names if wanted in name.lower()]


def _access_cell(page: Page, ip: str, name: str) -> Locator:
    """The Access cell of the row for this IP and account, found by what
    the row shows rather than its position, so a grid that re-renders
    can't move the click onto another server or account."""
    name_cell = page.locator(
        ACCOUNT_NAME_CELL_SELECTOR,
        has_text=re.compile(rf"^\s*{re.escape(name)}\s*$", re.IGNORECASE),
    )
    row = (
        page.locator(GRID_ROW_SELECTOR)
        .filter(has_text=_exact_ip(ip))
        .filter(has=name_cell)
    )
    return row.locator(ACCESS_CELL_SELECTOR)


def _select_and_access_one(
    page: Page, ip_filter: str, account: str | None, timeout_ms: int
) -> str | None:
    """Filter the grid to one IP and click Access on the right row.

    Returns the account name used, or None if the IP has no accounts. Raises
    if the requested account isn't there, or several accounts match it,
    rather than picking another one."""
    type_quick_filter(page, ip_filter, timeout_ms)
    row_count = wait_for_grid_settled(page, ip_filter, timeout_ms)
    grid = page.locator(GRID_ROW_SELECTOR)
    rows = _rows_for_ip([grid.nth(i) for i in range(row_count)], ip_filter, timeout_ms)
    if not rows:
        logger.warning("No accounts found for %s, skipping it", ip_filter)
        return None

    names = [_account_name(row, timeout_ms) for row in rows]
    if not account:
        name = names[0]
    else:
        matches = _matching_accounts(names, account)
        if len(matches) != 1:
            problem = (
                f"{len(matches)} accounts match {account!r} ({', '.join(matches)})"
                if matches
                else f"No account matching {account!r}"
            )
            raise RuntimeError(
                f"{problem} for {ip_filter}; nothing was requested for it. "
                "Give the full account name with -a or in the environment file."
            )
        name = matches[0]

    logger.info("Using account %s for %s", name, ip_filter)
    _access_cell(page, ip_filter, name).click(timeout=timeout_ms)
    return name


def _first_set(*values: T | None) -> T:
    """Return the first value that isn't None."""
    return next(value for value in values if value is not None)


def select_account_and_access(
    page: Page,
    targets: list[IPTarget],
    default_account: str | None,
    hours: int | None,
    reason: str | None,
    timeout_ms: int,
) -> bool:
    """Request access for each target, print a summary, then show Requests >
    Approved. True when done; False if the hours or reason prompt was
    cancelled. Raises if no IP had any accounts, or stops at the first IP
    that fails, after printing what was done so far.

    As a safeguard, any tab the portal opens meanwhile (such as one offering
    to start an SSH session) is closed as soon as it opens, and any left are
    closed at the end, even if the run stops early."""
    open_tabs = list(page.context.pages)
    page.context.on("page", _close_tab)
    try:
        return _request_all(page, targets, default_account, hours, reason, timeout_ms)
    finally:
        page.context.remove_listener("page", _close_tab)
        for tab in page.context.pages:
            if tab != page and tab not in open_tabs:
                tab.close()


def _request_all(
    page: Page,
    targets: list[IPTarget],
    default_account: str | None,
    hours: int | None,
    reason: str | None,
    timeout_ms: int,
) -> bool:
    open_directory_linked_accounts(page, timeout_ms)
    # Only ask for hours or reason if some target needs them. Cancelling
    # keeps the login open, the same as cancelling the IPs prompt.
    try:
        if any(t.hours is None for t in targets):
            hours = resolve_hours(hours)
        if any(t.reason is None for t in targets):
            reason = resolve_reason(reason)
    except (EOFError, KeyboardInterrupt):
        print("\nRequest cancelled — staying logged in.", file=sys.stderr)
        return False

    results: list[tuple[str, str, str]] = []  # (ip, account, outcome)
    for index, target in enumerate(targets, start=1):
        logger.info("Requesting access %d/%d: %s", index, len(targets), target.ip)
        account = target.account if target.account is not None else default_account
        try:
            name = _select_and_access_one(page, target.ip, account, timeout_ms)
            if name is None:
                results.append((target.ip, "—", "no accounts, skipped"))
                continue
            outcome = request_ssh_access(
                page,
                _first_set(target.hours, hours),
                _first_set(target.reason, reason),
                timeout_ms,
            )
        except Exception as exc:
            # Earlier IPs may already be requested: show them before stopping.
            results.append((target.ip, "—", "failed"))
            results.extend((t.ip, "—", "not attempted") for t in targets[index:])
            _print_results(results)
            raise RuntimeError(
                f"Stopped at {target.ip}: {str(exc) or type(exc).__name__}"
            ) from exc
        results.append((target.ip, name, outcome))

    _print_results(results)
    if all(name == "—" for _, name, _ in results):
        raise RuntimeError(
            "No accounts found in Directory Linked Accounts for "
            + ", ".join(t.ip for t in targets)
            + " — nothing was requested."
        )
    show_requests(page, timeout_ms)
    return True


def _print_results(results: list[tuple[str, str, str]]) -> None:
    ip_width = max(len(ip) for ip, _, _ in results)
    name_width = max(len(name) for _, name, _ in results)
    print("\nResults:")
    for ip, name, outcome in results:
        # Account names and outcomes come from the portal's pages.
        print(printable(f"  {ip:<{ip_width}}  {name:<{name_width}}  {outcome}"))


def _prompt_for_ips() -> list[IPTarget] | None:
    """Ask for IPs until at least one is given; None if cancelled."""
    while True:
        try:
            raw = input("IPs (comma-separated): ")
        except (EOFError, KeyboardInterrupt):
            print("Request cancelled — staying logged in.", file=sys.stderr)
            return None
        try:
            targets = parse_ip_list(raw)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            continue
        if targets:
            return targets
        print(
            "Nothing to request — enter IPs (e.g. 10.0.0.1,10.0.0.2) "
            "or retry with --env.",
            file=sys.stderr,
        )


def request_access(
    page: Page,
    env_config: EnvironmentConfig | None,
    ips: list[IPTarget] | None,
    account: str | None,
    hours: int | None,
    reason: str | None,
    timeout_ms: int,
) -> bool:
    """Request access to the servers from -e, -i or a prompt.

    Command-line values override the environment file's defaults. Returns
    True if anything was requested (False if a prompt was cancelled)."""
    targets: list[IPTarget] | None
    if env_config:
        targets = env_config.targets
        hours = hours if hours is not None else env_config.hours
        reason = reason if reason is not None else env_config.reason
        account = account if account is not None else env_config.account
    elif ips:
        targets = ips
    else:
        targets = _prompt_for_ips()
        if targets is None:
            return False
    return select_account_and_access(page, targets, account, hours, reason, timeout_ms)
