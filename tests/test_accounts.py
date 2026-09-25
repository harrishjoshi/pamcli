"""pam_cli.accounts: grid row matching and the access-request loop."""

import io
import unittest
from unittest.mock import MagicMock, call, patch

from playwright.sync_api import Error as PlaywrightError

from pam_cli.accounts import (
    _access_cell,
    _close_tab,
    _prompt_for_ips,
    _rows_for_ip,
    _select_and_access_one,
    open_directory_linked_accounts,
    request_access,
    select_account_and_access,
    show_requests,
    type_quick_filter,
    wait_for_grid_settled,
)
from pam_cli.config import ACCOUNT_NAME_CELL_SELECTOR
from pam_cli.environments import EnvironmentConfig, IPTarget


def _grid_page(rows_spec):
    """A page whose grid has one row per (account name, full row text)."""
    page = MagicMock()
    rows = []
    for account, text in rows_spec:
        row = MagicMock()
        row.inner_text.return_value = text
        row.locator.return_value.inner_text.return_value = account
        rows.append(row)
    page.locator.return_value.nth.side_effect = rows.__getitem__
    return page, rows


class NavigationTests(unittest.TestCase):
    def test_opens_directory_linked_accounts(self):
        page = MagicMock()
        open_directory_linked_accounts(page, 1000)
        self.assertEqual(
            page.get_by_role.call_args_list,
            [
                call("tab", name="Accounts", exact=True),
                call("button", name="Directory Linked Accounts", exact=True),
            ],
        )
        self.assertEqual(
            page.get_by_role.return_value.click.call_args_list,
            [call(timeout=1000)] * 2,
        )


class SelectAndAccessOneTests(unittest.TestCase):
    def _select(self, rows_spec, account=None, ip="10.0.0.1"):
        page, _ = _grid_page(rows_spec)
        with (
            patch("pam_cli.accounts.type_quick_filter"),
            patch(
                "pam_cli.accounts.wait_for_grid_settled", return_value=len(rows_spec)
            ),
            patch("pam_cli.accounts._access_cell") as access_cell,
        ):
            found = _select_and_access_one(page, ip, account, 1000)
        clicked = access_cell.return_value.click.called
        if clicked:
            access_cell.assert_called_once_with(page, ip, found)
        return found, clicked

    def test_only_rows_with_exactly_the_ip_count(self):
        # The Quick filter matches substrings: 10.0.0.1 also shows .15/.100.
        found, clicked = self._select(
            [
                ("app15", "app15 10.0.0.15 Access"),
                ("app1", "app1 10.0.0.1 Access"),
                ("app100", "app100 10.0.0.100 Access"),
            ]
        )
        self.assertEqual((found, clicked), ("app1", True))

    def test_skipped_when_no_row_has_exactly_the_ip(self):
        with self.assertLogs("pam_cli", "WARNING"):
            found, clicked = self._select([("app", "app 10.0.0.15 Access")])
        self.assertEqual((found, clicked), (None, False))

    def test_refuses_when_grid_shows_no_ips(self):
        # The rows can't be confirmed to be this server's: don't guess.
        with self.assertRaisesRegex(RuntimeError, "shows no IP addresses"):
            self._select([("app", "app Access")])

    def test_rows_for_ip_with_no_rows(self):
        self.assertEqual(_rows_for_ip([], "10.0.0.1", 1000), [])

    def test_exact_account_name_beats_a_containing_one(self):
        rows = [("devops-admin", "10.0.0.1"), ("DEV", "10.0.0.1")]
        self.assertEqual(self._select(rows, account="dev"), ("DEV", True))

    def test_a_single_containing_match_is_used(self):
        rows = [("svc.app01", "10.0.0.1"), ("root", "10.0.0.1")]
        self.assertEqual(self._select(rows, account="app01"), ("svc.app01", True))

    def test_several_containing_matches_raise_instead_of_guessing(self):
        # e.g. 'app' could otherwise request 'app-admin'.
        rows = [("svc.app01", "10.0.0.1"), ("app-admin", "10.0.0.1")]
        with self.assertRaisesRegex(
            RuntimeError, "2 accounts match 'app' \\(svc.app01, app-admin\\)"
        ):
            self._select(rows, account="app")

    def test_unmatched_account_raises_instead_of_picking_first_row(self):
        with self.assertRaisesRegex(RuntimeError, "No account matching 'nope'"):
            self._select([("app01", "10.0.0.1")], account="nope")


class AccessCellTests(unittest.TestCase):
    def test_found_by_exact_ip_and_account_not_by_position(self):
        page = MagicMock()
        _access_cell(page, "10.0.0.1", "svc.app01")
        grid = page.locator.return_value
        ip = grid.filter.call_args_list[0].kwargs["has_text"]
        self.assertTrue(ip.search("svc.app01 10.0.0.1 Access"))
        for other in ("10.0.0.15", "110.0.0.1", "10.0.0.100"):
            self.assertIsNone(ip.search(f"app {other} Access"), other)

        name_call = page.locator.call_args_list[0]
        self.assertEqual(name_call.args, (ACCOUNT_NAME_CELL_SELECTOR,))
        name = name_call.kwargs["has_text"]
        self.assertTrue(name.search(" SVC.app01 "))
        for other in ("svc.app011", "x svc.app01"):
            self.assertIsNone(name.search(other), other)
        grid.filter.return_value.filter.assert_called_once_with(
            has=page.locator.return_value
        )


@patch("pam_cli.polling.time.sleep")
class WaitForGridSettledTests(unittest.TestCase):
    def _page(self, counts, texts=None):
        page = MagicMock()
        rows = page.locator.return_value
        rows.count.side_effect = counts
        rows.all_inner_texts.side_effect = texts or (lambda: ["app 10.0.0.1 Access"])
        return page

    def test_needs_the_same_reading_twice(self, _sleep):
        # Old unfiltered rows first, then the filtered result, stable.
        page = self._page([40, 3, 3, 3])
        self.assertEqual(wait_for_grid_settled(page, "10.0.0.1", 1000), 3)

    def test_rows_left_from_the_previous_ip_are_not_settled(self, _sleep):
        # Same row count for both IPs: only the text shows the filter caught up.
        stale, fresh = ["a 10.0.0.1", "b 10.0.0.1"], ["a 10.0.0.2", "b 10.0.0.2"]
        page = self._page([2] * 4, [stale, stale, fresh, fresh])
        self.assertEqual(wait_for_grid_settled(page, "10.0.0.2", 1000), 2)
        self.assertEqual(page.locator.return_value.all_inner_texts.call_count, 4)

    def test_a_grid_without_ips_can_settle(self, _sleep):
        # It's rejected later, with a clearer error than a timeout.
        page = self._page([1, 1], [["app Access"]] * 2)
        self.assertEqual(wait_for_grid_settled(page, "10.0.0.1", 1000), 1)

    def test_no_records_message_means_zero(self, _sleep):
        page = MagicMock()
        page.locator.return_value.count.return_value = 0
        page.get_by_text.return_value.is_visible.return_value = True
        self.assertEqual(wait_for_grid_settled(page, "10.0.0.1", 1000), 0)

    def test_zero_rows_while_loading_is_not_settled(self, _sleep):
        page = MagicMock()
        page.locator.return_value.count.return_value = 0
        # Still loading, then the check fails, then "no records" twice.
        no_records = page.get_by_text.return_value.is_visible
        no_records.side_effect = [False, Exception("gone"), True, True]
        self.assertEqual(wait_for_grid_settled(page, "10.0.0.1", 1000), 0)
        self.assertEqual(no_records.call_count, 4)

    def test_raises_if_the_grid_never_settles(self, _sleep):
        with (
            patch("pam_cli.accounts.poll_until", return_value=None),
            self.assertRaisesRegex(RuntimeError, "didn't finish filtering to 10.0"),
        ):
            wait_for_grid_settled(MagicMock(), "10.0.0.1", 1000)


class ShowRequestsTests(unittest.TestCase):
    def test_opens_requests_then_approved(self):
        page = MagicMock()
        clicked = []
        requests_tab = page.get_by_role.return_value
        approved = requests_tab.or_.return_value.or_.return_value.first
        requests_tab.click.side_effect = lambda **_: clicked.append("Requests")
        approved.click.side_effect = lambda **_: clicked.append("Approved")

        show_requests(page, 1000)
        self.assertEqual(clicked, ["Requests", "Approved"])
        self.assertEqual(
            page.get_by_role.call_args_list,
            [
                call("tab", name="Approved", exact=True),
                call("button", name="Approved", exact=True),
                call("link", name="Approved", exact=True),
                call("tab", name="Requests", exact=True),
            ],
        )
        approved.click.assert_called_once_with(timeout=1000)

    def test_a_missing_tab_is_only_a_warning(self):
        for broken in ("Requests", "Approved"):
            with self.subTest(broken):
                page = MagicMock()
                requests_tab = page.get_by_role.return_value
                target = (
                    requests_tab
                    if broken == "Requests"
                    else requests_tab.or_.return_value.or_.return_value.first
                )
                target.click.side_effect = PlaywrightError("gone")
                with self.assertLogs("pam_cli", "WARNING") as logs:
                    show_requests(page, 1000)
                self.assertIn("Requests > Approved", logs.output[0])


class TabClosingTests(unittest.TestCase):
    """The portal opens a tab per request offering to start the SSH session;
    pamcli closes every such tab, whenever it opens."""

    def _run(self, request_all):
        page, users_tab = MagicMock(), MagicMock()
        page.context.pages = [page, users_tab]  # open before the requests
        with patch("pam_cli.accounts._request_all", side_effect=request_all):
            try:
                return (
                    page,
                    users_tab,
                    select_account_and_access(
                        page, [IPTarget(ip="10.0.0.1")], None, 4, "x", 1000
                    ),
                )
            finally:
                page.context.remove_listener.assert_called_once_with("page", _close_tab)

    def test_tabs_are_closed_as_they_open_and_at_the_end(self):
        opened, straggler = MagicMock(), MagicMock()

        def request_all(page, *_args):
            page.context.on.assert_called_once_with("page", _close_tab)
            _close_tab(opened)  # what the "page" event does
            page.context.pages.append(straggler)
            return True

        page, users_tab, result = self._run(request_all)
        self.assertTrue(result)
        opened.close.assert_called_once_with()
        straggler.close.assert_called_once_with()
        users_tab.close.assert_not_called()
        page.close.assert_not_called()  # the portal page stays

    def test_tabs_are_closed_even_when_the_run_stops(self):
        straggler = MagicMock()

        def request_all(page, *_args):
            page.context.pages.append(straggler)
            raise RuntimeError("Stopped at 10.0.0.1")

        with self.assertRaisesRegex(RuntimeError, "Stopped at"):
            self._run(request_all)
        straggler.close.assert_called_once_with()


class QuickFilterTests(unittest.TestCase):
    def test_clears_the_previous_ip_before_typing(self):
        page = MagicMock()
        type_quick_filter(page, "10.0.0.1", 1000)
        quick_filter = page.get_by_placeholder.return_value
        quick_filter.fill.assert_called_once_with("", timeout=1000)  # clears first
        quick_filter.press_sequentially.assert_called_once_with("10.0.0.1", delay=30)


class SelectAccountAndAccessTests(unittest.TestCase):
    def _run(self, targets, found="svc.app0", account="svc.app0"):
        with (
            patch("sys.stdout", new_callable=io.StringIO) as self.output,
            patch("pam_cli.accounts.open_directory_linked_accounts"),
            patch("pam_cli.accounts.resolve_hours", return_value=4) as self.ask_hours,
            patch(
                "pam_cli.accounts.resolve_reason", return_value="UAT"
            ) as self.ask_reason,
            patch(
                "pam_cli.accounts._select_and_access_one",
                side_effect=found if isinstance(found, list) else None,
                return_value=found,
            ) as select,
            patch(
                "pam_cli.accounts.request_ssh_access", return_value="requested"
            ) as start,
            patch("pam_cli.accounts.show_requests") as self.show_requests,
        ):
            result = select_account_and_access(
                MagicMock(), targets, account, None, None, 1000
            )
        return result, select, start

    def test_per_target_overrides_beat_defaults(self):
        targets = [
            IPTarget(ip="10.0.0.1"),
            IPTarget(ip="10.0.0.2", hours=2, reason="db", account="db01"),
        ]
        result, select, start = self._run(targets)
        self.assertTrue(result)
        self.assertEqual(
            [c.args[1:3] for c in select.call_args_list],
            [("10.0.0.1", "svc.app0"), ("10.0.0.2", "db01")],
        )
        self.assertEqual(
            [c.args[1:3] for c in start.call_args_list], [(4, "UAT"), (2, "db")]
        )

    def test_no_prompt_when_every_target_sets_its_own_values(self):
        targets = [IPTarget(ip="10.0.0.1", hours=2, reason="db")]
        _, _, start = self._run(targets)
        self.ask_hours.assert_not_called()
        self.ask_reason.assert_not_called()
        self.assertEqual(start.call_args.args[1:3], (2, "db"))

    def test_prints_a_result_per_ip_then_shows_requests(self):
        # The second IP has no accounts: it's skipped, the batch carries on.
        targets = [IPTarget(ip="10.0.0.1"), IPTarget(ip="10.0.0.20")]
        result, _, start = self._run(targets, found=["svc.app01", None])
        self.assertTrue(result)
        self.assertEqual(start.call_count, 1)
        self.show_requests.assert_called_once()
        self.assertEqual(
            self.output.getvalue().splitlines()[-2:],
            [
                "  10.0.0.1   svc.app01  requested",
                "  10.0.0.20  —          no accounts, skipped",
            ],
        )

    def test_cancelling_a_prompt_keeps_the_login(self):
        for error in (KeyboardInterrupt, EOFError):
            with (
                self.subTest(error.__name__),
                patch("pam_cli.accounts.open_directory_linked_accounts"),
                patch("pam_cli.accounts.resolve_hours", side_effect=error),
                patch("pam_cli.accounts._select_and_access_one") as select,
                patch("builtins.print"),
            ):
                result = select_account_and_access(
                    MagicMock(), [IPTarget(ip="10.0.0.1")], None, None, None, 1000
                )
            self.assertFalse(result)
            select.assert_not_called()  # nothing was requested

    def test_a_failure_stops_the_batch_but_shows_what_was_requested(self):
        targets = [IPTarget(ip=f"10.0.0.{n}") for n in (1, 2, 3)]
        with self.assertRaisesRegex(
            RuntimeError, "Stopped at 10.0.0.2: No account matching"
        ):
            self._run(targets, found=["svc.app01", RuntimeError("No account matching")])
        self.assertEqual(
            self.output.getvalue().splitlines()[-3:],
            [
                "  10.0.0.1  svc.app01  requested",
                "  10.0.0.2  —          failed",
                "  10.0.0.3  —          not attempted",
            ],
        )
        self.show_requests.assert_not_called()

    def test_results_cant_control_the_terminal(self):
        self._run([IPTarget(ip="10.0.0.1")], found="app\x1b[2K")
        self.assertIn("app\\x1b[2K  requested", self.output.getvalue())

    def test_raises_when_no_target_had_accounts(self):
        # A batch where every IP is skipped must not look like a success.
        for targets in (
            [IPTarget(ip="10.0.0.1")],
            [IPTarget(ip="10.0.0.1"), IPTarget(ip="10.0.0.2")],
        ):
            with (
                self.subTest(count=len(targets)),
                self.assertRaisesRegex(
                    RuntimeError, "No accounts found.* for 10.0.0.1"
                ),
            ):
                self._run(targets, found=[None] * len(targets))


@patch("pam_cli.accounts.select_account_and_access", return_value=True)
class RequestAccessTests(unittest.TestCase):
    def test_cli_values_beat_env_file_defaults(self, select):
        env = EnvironmentConfig(
            targets=[IPTarget(ip="10.0.0.1")], hours=6, reason="file", account="db"
        )
        self.assertTrue(request_access(MagicMock(), env, None, None, 2, None, 1000))
        _, targets, account, hours, reason, _ = select.call_args.args
        self.assertEqual(
            (targets, account, hours, reason), (env.targets, "db", 2, "file")
        )

    def test_ips_from_the_command_line_skip_the_prompt(self, select):
        ips = [IPTarget(ip="10.0.0.1")]
        with patch("pam_cli.accounts._prompt_for_ips") as prompt:
            self.assertTrue(request_access(MagicMock(), None, ips, None, 2, "x", 1000))
        prompt.assert_not_called()
        self.assertIs(select.call_args.args[1], ips)

    def test_prompt_cancel_stays_logged_in(self, select):
        with patch("pam_cli.accounts._prompt_for_ips", return_value=None):
            self.assertFalse(
                request_access(MagicMock(), None, None, None, None, None, 1000)
            )
        select.assert_not_called()


class PromptForIpsTests(unittest.TestCase):
    def test_reprompts_until_an_ip_is_given(self):
        with (
            patch(
                "builtins.input", side_effect=["", " , ", "10.0.0", "10.0.0.5"]
            ) as prompt,
            patch("builtins.print") as printed,
        ):
            self.assertEqual(_prompt_for_ips(), [IPTarget(ip="10.0.0.5")])
        self.assertEqual(prompt.call_count, 4)
        self.assertIn("is not an IPv4 address", str(printed.call_args_list))

    def test_eof_cancels(self):
        with patch("builtins.input", side_effect=EOFError), patch("builtins.print"):
            self.assertIsNone(_prompt_for_ips())


if __name__ == "__main__":
    unittest.main()
