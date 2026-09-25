"""pam_cli.logging_setup: the `[time LEVEL pamcli::module] message` format."""

import io
import logging
import re
import unittest
from unittest.mock import patch

from pam_cli.logging_setup import printable, setup_logging


class LogFormatTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(logging.getLogger("pam_cli").handlers.clear)

    def _log(self, verbose):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            setup_logging(verbose)  # the handler binds to the patched stderr
            logger = logging.getLogger("pam_cli.accounts")
            logger.debug("debug detail")
            logger.info("Requesting access 1/2")
            logger.warning("No accounts found")
        return err.getvalue().splitlines()

    def test_format(self):
        lines = self._log(verbose=False)
        stamp = r"\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ "
        self.assertRegex(
            lines[0], stamp + r"INFO  pamcli::accounts\] Requesting access 1/2$"
        )
        self.assertRegex(
            lines[1], stamp + r"WARN  pamcli::accounts\] No accounts found$"
        )

    def test_debug_only_with_verbose(self):
        self.assertEqual(len(self._log(verbose=False)), 2)
        lines = self._log(verbose=True)
        self.assertEqual(len(lines), 3)
        self.assertTrue(re.search(r"DEBUG pamcli::accounts\] debug detail$", lines[0]))

    def test_page_text_cant_control_the_terminal(self):
        # e.g. an account name that clears the line or reverses the text.
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            setup_logging()
            logging.getLogger("pam_cli.accounts").info("Using \x1b[2Kapp\r\u202e")
        self.assertIn("Using \\x1b[2Kapp\\r\\u202e", err.getvalue())

    def test_printable_keeps_newlines_tabs_and_text(self):
        self.assertEqual(printable("a\tb\nc — é"), "a\tb\nc — é")
        self.assertEqual(printable("\x00\x9b\u2066"), "\\x00\\x9b\\u2066")
        # Invisible direction marks and line separators, too.
        self.assertEqual(
            printable("\u200e\u200f\u061c\u2028\u2029"),
            "\\u200e\\u200f\\u061c\\u2028\\u2029",
        )

    def test_exception_lines_follow_the_message(self):
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            setup_logging(verbose=True)
            logger = logging.getLogger("pam_cli.accounts")
            try:
                raise ValueError("kaboom")
            except ValueError:
                logger.exception("boom")
        lines = err.getvalue().splitlines()
        stamp = r"\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ "
        self.assertRegex(lines[0], stamp + r"ERROR pamcli::accounts\] boom$")
        self.assertIn("Traceback (most recent call last):", lines[1])
        self.assertIn("ValueError: kaboom", lines[-1])


if __name__ == "__main__":
    unittest.main()
