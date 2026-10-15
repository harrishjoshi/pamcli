"""Finding servers in the accounts grid and requesting access to them."""

import logging
import re
import sys
from typing import TypeVar

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from pam_cli.access_dialog import (
    request_access_in_dialog,
    resolve_hours,
    resolve_reason,
)
from pam_cli.config import (
    ACCESS_CELL_SELECTOR,
    ACCOUNT_NAME_CELL_SELECTOR,
    DEFAULT_ACCESS,
    DIRECTORY_CELL_SELECTOR,
    GRID_ROW_SELECTOR,
)
from pam_cli.environments import EnvironmentConfig, IPTarget, parse_ip_list
from pam_cli.logging_setup import printable
from pam_cli.polling import poll_until

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
    """True if the rows are the Quick filter's result for this IP: every row
    contains it. Rows left over from the previous IP don't, and neither do
    rows the grid has drawn but not filled in yet."""
    return all(ip in text for text in texts)


def _shows_no_ips(texts: list[str]) -> bool:
    """True if the rows have text but no IP address in any of them, e.g.
    when the grid's IP column is hidden."""
    return any(text.strip() for text in texts) and not any(
        _IPV4.search(text) for text in texts
    )


def _no_ips_error(ip: str) -> RuntimeError:
    return RuntimeError(
        f"The accounts grid shows no IP addresses, so its rows can't be "
        f"confirmed to belong to {ip}; nothing was requested for it."
    )


def wait_for_grid_settled(page: Page, ip: str, timeout_ms: int) -> int:
    """Wait for the grid to finish filtering to this IP and return its row
    count.

    The filter can lag behind typing, so the rows have to be the filter's
    result for this IP, with the same count on two checks in a row, before
    they're trusted."""
    rows = page.locator(GRID_ROW_SELECTOR)
    previous: list[int] = []
    texts: list[str] = []

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
        else:
            texts[:] = rows.all_inner_texts()
            if not _shows_filter_result(texts, ip):
                count = -1  # the previous IP's rows, or rows still being drawn
        settled = count >= 0 and previous == [count]
        previous[:] = [count]
        return count if settled else None

    settled = poll_until(check, timeout_ms, poll_interval=0.3)
    if settled is None:
        if _shows_no_ips(texts):
            raise _no_ips_error(ip)
        raise RuntimeError(f"The accounts grid didn't finish filtering to {ip}.")
    logger.debug("The grid shows %d row(s) for %s", settled, ip)
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
        raise _no_ips_error(ip)
    exact = _exact_ip(ip)
    return [row for row, text in zip(rows, texts) if exact.search(text)]


def _account_name(row: Locator, timeout_ms: int) -> str:
    return (
        row.locator(ACCOUNT_NAME_CELL_SELECTOR).inner_text(timeout=timeout_ms).strip()
    )


def _matching_accounts(names: list[str], account: str) -> list[str]:
    """The account names that match: the exact name (ignoring case) if it's
    there, else every name containing it (e.g. 'app01' in 'svc.app01').
    The same spelling shown on several rows is listed once."""
    wanted = account.strip().lower()
    exact = [name for name in names if name.lower() == wanted]
    return list(
        dict.fromkeys(exact or [name for name in names if wanted in name.lower()])
    )


# Set on each row of an account listed several times, so a retry clicks the
# row it means even if the grid reorders them. pamcli's own, not the portal's.
_COPY_ATTRIBUTE = "data-pamcli-copy"


def _account_rows(page: Page, ip: str, name: str) -> Locator:
    """The rows for this IP and account, found by what they show rather than
    their position, so a grid that re-renders can't move a click onto
    another server or account."""
    name_cell = page.locator(
        ACCOUNT_NAME_CELL_SELECTOR,
        has_text=re.compile(rf"^\s*{re.escape(name)}\s*$"),
    )
    return (
        page.locator(GRID_ROW_SELECTOR)
        .filter(has_text=_exact_ip(ip))
        .filter(has=name_cell)
    )


def _access_cell(page: Page, ip: str, name: str, copy: int | None = None) -> Locator:
    """The Access cell of the row for this IP and account. `copy` picks one
    of the rows marked by _mark_copies when the account has several."""
    row = _account_rows(page, ip, name)
    if copy is not None:
        row = row.and_(page.locator(f"[{_COPY_ATTRIBUTE}='{copy}']"))
    return row.locator(ACCESS_CELL_SELECTOR)


def _mark_copies(page: Page, ip: str, name: str, copies: int, timeout_ms: int) -> None:
    """Number the account's rows, so each is tried once even if the grid
    reorders them while the others are tried."""
    rows = _account_rows(page, ip, name)
    if rows.count() != copies:
        raise _grid_changed_error()
    for copy in range(copies):
        rows.nth(copy).evaluate(
            f"(row, copy) => row.setAttribute('{_COPY_ATTRIBUTE}', String(copy))",
            copy,
            timeout=timeout_ms,
        )


def _grid_changed_error() -> RuntimeError:
    return RuntimeError(
        "The accounts grid changed while its rows for this account were being "
        "tried; nothing more was requested for it."
    )


def _row_text(row: Locator, timeout_ms: int) -> str:
    return " ".join(row.inner_text(timeout=timeout_ms).split())


def _row_identity(row: Locator, timeout_ms: int) -> str:
    """What tells this row's account from another with the same name: its
    directory. Other columns, such as the status, can differ between rows
    of one account. If the directory column is hidden, the whole row is
    compared instead, so accounts are never merged on a guess."""
    directory = row.locator(DIRECTORY_CELL_SELECTOR)
    if directory.count() == 0:
        return _row_text(row, timeout_ms)
    return " ".join(directory.inner_text(timeout=timeout_ms).split())


def _find_account(
    page: Page, ip_filter: str, account: str | None, timeout_ms: int
) -> tuple[str, int] | None:
    """Filter the grid to one IP and find the account to request.

    Returns the account name and how many rows show it (the portal can list
    the same account more than once, each row offering different access
    types), or None if the IP has no accounts. Raises if the requested
    account isn't there, several accounts match it, or its rows differ,
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

    # Rows count as one account only if they're in the same directory, so
    # two accounts sharing a name in different directories aren't merged.
    same = [row for row, other in zip(rows, names) if other == name]
    if len({_row_identity(row, timeout_ms) for row in same}) > 1:
        raise RuntimeError(
            f"{len(same)} rows for {ip_filter} show account {name!r} as "
            "different accounts (e.g. in different directories), so which one "
            "to request is unclear; nothing was requested for it."
        )
    logger.info("Using account %s for %s", name, ip_filter)
    return name, len(same)


def _request_account(
    page: Page,
    ip: str,
    name: str,
    copies: int,
    hours: int,
    reason: str,
    access: str,
    timeout_ms: int,
) -> str:
    """Click Access on the account's row and request the access there.

    The portal can list an account on several rows, each offering different
    access types. Then each row is tried in turn until one takes the request.
    For `both`, only a row offering both switches is accepted. The first row
    with an active request ends the search, so the account is never requested
    twice."""
    if copies < 1:
        raise ValueError(f"Invalid row count {copies} for account {name!r}")
    if copies == 1:
        _access_cell(page, ip, name).click(timeout=timeout_ms)
        return request_access_in_dialog(page, hours, reason, timeout_ms, access=access)

    require_both = access == "both"
    _mark_copies(page, ip, name, copies, timeout_ms)
    for copy in range(copies):
        logger.info("Trying row %d of %d for the account", copy + 1, copies)
        cell = _access_cell(page, ip, name, copy)
        # A row the grid has drawn again has lost its mark: stop, not guess.
        if cell.count() != 1:
            raise _grid_changed_error()
        cell.click(timeout=timeout_ms)
        status, outcome = request_access_in_dialog(
            page,
            hours,
            reason,
            timeout_ms,
            access=access,
            require_both=require_both,
            with_status=True,
        )
        if status != "skipped":
            return outcome
    if require_both:
        logger.warning(
            "None of the %d rows for the account offers both the passkey and "
            "SSH; skipping it",
            copies,
        )
        return "skipped (no row offers both the passkey and SSH)"
    return outcome


def _first_set(*values: T | None) -> T:
    """Return the first value that isn't None."""
    return next(value for value in values if value is not None)


def select_account_and_access(
    page: Page,
    targets: list[IPTarget],
    default_account: str | None,
    hours: int | None,
    reason: str | None,
    access: str | None,
    timeout_ms: int,
) -> bool:
    """Request access for each target, print a summary, then show Requests >
    Approved. True when done; False if the hours or reason prompt was
    cancelled, or if every IP was skipped (with a warning). Stops at the
    first IP that fails, after printing what was done so far.

    As a safeguard, any tab the portal opens meanwhile (such as one offering
    to start an SSH session) is closed as soon as it opens, and any left are
    closed at the end, even if the run stops early."""
    open_tabs = list(page.context.pages)
    page.context.on("page", _close_tab)
    try:
        return _request_all(
            page, targets, default_account, hours, reason, access, timeout_ms
        )
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
    access: str | None,
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
            found = _find_account(page, target.ip, account, timeout_ms)
            if found is None:
                results.append((target.ip, "—", "no accounts, skipped"))
                continue
            name, copies = found
            outcome = _request_account(
                page,
                target.ip,
                name,
                copies,
                _first_set(target.hours, hours),
                _first_set(target.reason, reason),
                _first_set(target.access, access, DEFAULT_ACCESS),
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
        logger.warning(
            "No accounts found in Directory Linked Accounts for %s — nothing "
            "was requested.",
            ", ".join(t.ip for t in targets),
        )
        return False
    if all(
        name == "—" or outcome.startswith("skipped") for _, name, outcome in results
    ):
        logger.warning(
            "Every IP was skipped (see the results above) — nothing was requested."
        )
        return False
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
    access: str | None,
    timeout_ms: int,
) -> bool:
    """Request access to the servers from -e, -i or a prompt.

    Command-line values override the environment file's defaults. Returns
    True if anything was requested (False if a prompt was cancelled or every
    IP was skipped)."""
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
    return select_account_and_access(
        page, targets, account, hours, reason, access, timeout_ms
    )
