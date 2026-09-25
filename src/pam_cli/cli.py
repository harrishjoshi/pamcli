"""Log into a PAM portal and request SSH access to servers."""

import argparse
import logging
import math
import os
import shutil
import subprocess
import sys
from importlib.metadata import version

from playwright.sync_api import BrowserContext, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from pam_cli.accounts import request_access
from pam_cli.auth import do_login, resolve_url, take_env_password
from pam_cli.config import (
    DEFAULT_TIMEOUT_MS,
    MAX_SESSION_HOURS,
)
from pam_cli.discover import discover
from pam_cli.environments import (
    EnvironmentConfig,
    IPTarget,
    load_environment,
    parse_ip_list,
)
from pam_cli.logging_setup import printable, setup_logging
from pam_cli.reachability import check_reachable, is_cert_problem
from pam_cli.shell_rc import offer_to_save

logger = logging.getLogger(__name__)


def _error(message: str) -> None:
    """Print an error. Errors can quote the portal or the network, so they're
    made safe to show in a terminal first."""
    print(printable(f"ERROR: {message}"), file=sys.stderr)


def _install_chromium() -> bool:
    """Download Playwright's Chromium. False if that fails (e.g. offline)."""
    print("Installing Playwright's Chromium (one-time)...", flush=True)
    # Use pamcli's own Python: pipx doesn't put `playwright` on PATH.
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("Installing Chromium failed: %s", exc)
        return False
    return True


def _confirm(question: str) -> bool:
    """Ask yes/no at the terminal; no terminal to answer counts as no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _install_system_deps() -> bool:
    """Install the Linux libraries Chromium needs, asking before using sudo.
    False if that isn't possible or the answer is no."""
    if not sys.platform.startswith("linux"):
        return False
    command = [sys.executable, "-m", "playwright", "install-deps", "chromium"]
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo or not _confirm(
            "Chromium needs system libraries. Install them now with "
            f"`sudo {' '.join(command)}`? [y/N] "
        ):
            return False
        command = [sudo, *command]
    print("Installing system libraries Chromium needs (one-time)...", flush=True)
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("Installing system libraries failed: %s", exc)
        return False
    return True


def _open_browser(playwright: Playwright, headless: bool = False) -> BrowserContext:
    """Start Chromium with a fresh private session: visible and maximized, or
    headless (for `pamcli setup`, which only checks that it starts).

    Cookies stay in memory, so nothing is kept after the run. If Chromium or
    its Linux libraries are missing, they're installed once and the launch
    is retried."""
    installed_chromium = installed_deps = False
    while True:
        try:
            browser = playwright.chromium.launch(
                headless=headless, args=[] if headless else ["--start-maximized"]
            )
        except PlaywrightError as exc:
            message = str(exc).lower()
            if (
                "executable doesn't exist" in message
                and not installed_chromium
                and _install_chromium()
            ):
                installed_chromium = True
            elif (
                "missing dependencies" in message
                and not installed_deps
                and _install_system_deps()
            ):
                installed_deps = True
            else:
                raise
        else:
            # Let the page fill the window (Playwright otherwise fixes it at
            # 1280x720).
            return browser.new_context(no_viewport=True)


def _launch_browser(playwright: Playwright) -> BrowserContext | None:
    """The browser context, or None after reporting a launch failure."""
    try:
        return _open_browser(playwright)
    except PlaywrightError as exc:
        _error(f"could not start Chromium ({exc}).")
        return None


def _setup() -> int:
    """Offer to save PAM_URL and PAM_USERNAME in the shell's start-up file, then
    download Chromium and check that it starts, installing the Linux system
    libraries it needs if they're missing (asking before using sudo)."""
    offer_to_save()
    if not _install_chromium():
        _error("could not download Chromium; see the messages above.")
        return 1
    with sync_playwright() as playwright:
        try:
            _open_browser(playwright, headless=True).close()
        except PlaywrightError as exc:
            _error(f"Chromium was downloaded but doesn't start ({exc}).")
            return 1
    print("Chromium is ready. Next: pamcli login")
    return 0


def _hours_in_range(value: str) -> int:
    if not value.isdecimal() or not 1 <= int(value) <= MAX_SESSION_HOURS:
        raise argparse.ArgumentTypeError(
            f"must be a whole number from 1 to {MAX_SESSION_HOURS}, got {value!r}"
        )
    return int(value)


def _ip_list(value: str) -> list[IPTarget]:
    try:
        targets = parse_ip_list(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if not targets:
        raise argparse.ArgumentTypeError(f"no IPs in {value!r}")
    return targets


def _non_blank(value: str) -> str:
    # A blank account would match every row and silently pick the first; a
    # blank reason would leave the access request without one.
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be blank")
    return value


# All help text starts in this column, so options, commands and the `#` of
# example comments line up. It fits the longest option, `-a NAME, --account NAME`,
# and the longest example, each with room to spare.
_HELP_COLUMN = 29


def _examples(*examples: tuple[str, str]) -> str:
    """Format help examples so their `#` comments start in the option help's
    column."""
    lines = [
        f"  {command}".ljust(_HELP_COLUMN) + f"# {comment}"
        for command, comment in examples
    ]
    return "\n".join(["examples:", *lines])


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that starts every description in the same column.

    On its own, argparse picks a different column for each help screen and
    misjudges how wide command names are, which made `request (req)` wrap."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=_HELP_COLUMN)
        self._action_max_length = _HELP_COLUMN - 2

    def add_argument(self, action: argparse.Action) -> None:
        super().add_argument(action)
        if action.help is argparse.SUPPRESS or not hasattr(action, "_get_subactions"):
            return
        indent = self._current_indent + self._indent_increment
        for command in action._get_subactions():
            width = len(self._format_action_invocation(command)) + indent
            self._action_max_length = max(self._action_max_length, width)


_VERBOSE_HELP = "show detailed logs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pamcli",
        description=__doc__,
        epilog=_examples(
            ("pamcli setup", "save settings and download Chromium"),
            ("pamcli login", "log in and keep the Chromium window open"),
            ("pamcli req -e UAT", "request every IP in the UAT environment file"),
            ("pamcli discover", "if login stops working after a portal update"),
        )
        + "\n\nrun 'pamcli COMMAND -h' for a command's options.",
        formatter_class=_HelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {version('pamcli')}"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help=_VERBOSE_HELP)
    parser.set_defaults(command=None)
    # Allow -v after the command too, without it turning off a -v given
    # before the command.
    command_options = argparse.ArgumentParser(add_help=False)
    command_options.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help=_VERBOSE_HELP,
    )
    commands = parser.add_subparsers(title="commands", metavar="COMMAND")

    commands.add_parser(
        "setup",
        help="save settings and download Chromium",
        description="Offer to save PAM_URL and PAM_USERNAME in the shell's start-up "
        "file,\nif they aren't set yet, then download Chromium (and, on Linux, the\n"
        "libraries it needs). Optional; safe to run again.",
        formatter_class=_HelpFormatter,
        parents=[command_options],
        allow_abbrev=False,
    ).set_defaults(command="setup")

    commands.add_parser(
        "login",
        help="log in and keep the Chromium window open",
        description="Log in and keep the Chromium window open until it's closed.",
        formatter_class=_HelpFormatter,
        parents=[command_options],
        allow_abbrev=False,
    ).set_defaults(command="login")

    request = commands.add_parser(
        "request",
        aliases=["req"],
        help="log in, then request SSH access to servers",
        description="Log in, then request SSH access to servers.\n"
        "Without -e or -i, pamcli asks for the IPs.",
        epilog=_examples(
            ("pamcli req", "asks for IPs"),
            ("pamcli req -e UAT", "every IP in UAT.json"),
            ("pamcli req -i 10.0.0.1", "one IP"),
        ),
        formatter_class=_HelpFormatter,
        parents=[command_options],
        allow_abbrev=False,
    )
    request.set_defaults(command="request")
    targets = request.add_mutually_exclusive_group()
    targets.add_argument(
        "-e",
        "--env",
        metavar="NAME",
        help="request each IP in the environment file NAME.json",
    )
    targets.add_argument(
        "-i",
        "--ips",
        type=_ip_list,
        metavar="IPs",
        help="request these IPs (comma-separated)",
    )
    request.add_argument(
        "-a",
        "--account",
        type=_non_blank,
        metavar="NAME",
        help="PAM account to request, e.g. svc.app01",
    )
    request.add_argument(
        "-H",
        "--hours",
        type=_hours_in_range,
        metavar="N",
        help=f"session length, 1-{MAX_SESSION_HOURS} hours (asked if not set)",
    )
    request.add_argument(
        "-r",
        "--reason",
        type=_non_blank,
        metavar="TEXT",
        help="reason for the access request (asked if not set)",
    )

    commands.add_parser(
        "discover",
        help="list the login page's fields, if login breaks",
        description="Write the login page's fields to login_page_fields.txt and\n"
        "save a screenshot, for when login stops working after a portal update.",
        formatter_class=_HelpFormatter,
        parents=[command_options],
        allow_abbrev=False,
    ).set_defaults(command="discover")
    return parser


def _timeout_ms() -> int:
    """The page timeout in milliseconds, from PAM_TIMEOUT (seconds) if set."""
    raw = os.environ.get("PAM_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_MS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0
    # Under a second is surely a mistake, and 0 ms would mean "no timeout"
    # to Playwright.
    if not (seconds >= 1 and math.isfinite(seconds)):
        raise ValueError(
            f"PAM_TIMEOUT must be a number of seconds, at least 1, got {raw!r}."
        )
    return int(seconds * 1000)


# Each makes Playwright print or show what it sends to the browser, which
# includes the typed password and TOTP code.
_PLAYWRIGHT_DEBUG_VARIABLES = ("DEBUG", "DEBUGP", "PWDEBUG")


def _drop_playwright_debug() -> None:
    """Remove Playwright's debug variables before it starts, with a warning."""
    dropped = [
        name for name in _PLAYWRIGHT_DEBUG_VARIABLES if os.environ.pop(name, None)
    ]
    if dropped:
        logger.warning(
            "Ignoring %s: Playwright would log the password and TOTP code",
            ", ".join(dropped),
        )


def _wait_for_window_close(context: BrowserContext) -> None:
    """Wait until the window is closed, i.e. every tab in it has closed.

    Chromium keeps running after its window is closed, so the tabs are what
    show that the window has gone."""
    try:
        while context.pages:
            context.pages[0].wait_for_event("close", timeout=0)
    except Exception as exc:
        logger.debug("Waiting for the window to close ended early: %s", exc)


def main() -> int:
    try:
        return _run()
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


def _run() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "setup":
        return _setup()  # needs no PAM_* settings, so it works before they're set
    is_request = args.command == "request"

    try:
        timeout_ms = _timeout_ms()
        # Load the environment file first, so a bad one fails before any prompts.
        env_config: EnvironmentConfig | None = (
            load_environment(args.env) if is_request and args.env else None
        )
        url = resolve_url()
        env_password = take_env_password()
        _drop_playwright_debug()
    except (RuntimeError, ValueError) as exc:
        _error(str(exc))
        return 1

    problem = check_reachable(url)
    if problem is not None and is_cert_problem(problem):
        _error(
            f"The portal's {problem}. If the portal opens in a normal "
            "browser, its certificate chain may be incomplete, or its CA may "
            "be missing from Python's certificates (set SSL_CERT_FILE to a CA "
            "bundle that includes it)."
        )
        return 1
    if problem is not None:
        _error(
            f"The PAM portal could not be reached ({problem}). "
            "Check the VPN or network connection."
        )
        return 1

    with sync_playwright() as p:
        context = _launch_browser(p)
        if context is None:
            return 1
        # Covers every page action that isn't given its own timeout.
        context.set_default_timeout(timeout_ms)
        page = context.new_page()

        if args.command == "discover":
            try:
                discover(page, url, timeout_ms)
            except Exception as exc:
                _error(str(exc) or type(exc).__name__)
                return 1
            finally:
                context.close()
            return 0

        try:
            reached_url = do_login(page, url, timeout_ms, env_password)
            requested = (
                request_access(
                    page,
                    env_config,
                    args.ips,
                    args.account,
                    args.hours,
                    args.reason,
                    timeout_ms,
                )
                if is_request
                else False
            )
        except Exception as exc:
            # Keep the window open: the page (and any screenshot) shows what went
            # wrong. Some errors, like a closed input, have no message.
            _error(str(exc) or type(exc).__name__)
            print(
                "Leaving the Chromium window open to show what happened; "
                "close it to exit.",
                flush=True,
            )
            _wait_for_window_close(context)
            context.close()
            return 1

        message = (
            "Access requested — the Chromium window shows Requests > Approved"
            if requested
            else f"Logged in — the Chromium window stays open on {reached_url}"
        )
        print(printable(f"{message}; close it to exit."), flush=True)
        _wait_for_window_close(context)
        context.close()
        return 0
