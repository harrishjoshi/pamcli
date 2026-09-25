"""pam_cli.discover: the login page's fields report, against a mocked page."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pam_cli.discover import discover

URL = "https://pam.example.com/login"
GUID = "00000000-0000-4000-8000-000000000000"


def _element(text, **attributes):
    element = MagicMock(**{"inner_text.return_value": text})
    element.get_attribute.side_effect = attributes.get
    return element


@patch("pam_cli.discover.dismiss_authorized_use_banner")
class DiscoverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        cwd = os.getcwd()
        os.chdir(self.dir)  # the report and screenshot go into the cwd
        self.addCleanup(os.chdir, cwd)

        self.page = MagicMock(url=URL)
        self.page.screenshot.return_value = b"png"
        elements = {
            "input": [
                _element("", name="login-id", id=GUID, type="text", placeholder=" ")
            ],
            "button": [
                _element("Continue", type="submit"),
                _element("", type="button", id=GUID),  # icon-only: nothing to match
            ],
            "label": [_element("Login ID", **{"for": GUID})],
        }
        self.page.query_selector_all.side_effect = elements.__getitem__

    def _discover(self):
        with redirect_stdout(io.StringIO()) as out:
            discover(self.page, URL, 5000)
        return out.getvalue()

    def test_page_text_in_the_report_cant_control_the_terminal(self, dismiss):
        self.page.query_selector_all.side_effect = lambda tag: (
            [_element("Con\x1b[2Jtinue", type="submit")] if tag == "button" else []
        )
        self._discover()
        report = (self.dir / "login_page_fields.txt").read_text()
        self.assertIn("Con\\x1b[2Jtinue", report)
        self.assertNotIn("\x1b", report)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "needs O_NOFOLLOW")
    def test_refuses_to_write_through_a_planted_symlink(self, dismiss):
        # e.g. login_page_fields.txt -> ~/.bashrc, left in a shared folder.
        for name in ("login_page_fields.txt", "login_page.png"):
            with self.subTest(name):
                victim = self.dir / f"victim-{name}"
                victim.write_text("keep me")
                (self.dir / name).symlink_to(victim)
                with self.assertRaises(OSError):
                    self._discover()
                self.assertEqual(victim.read_text(), "keep me")
                (self.dir / name).unlink()

    def test_writes_useful_fields_and_a_screenshot(self, dismiss):
        output = self._discover()
        report = (self.dir / "login_page_fields.txt").read_text()
        # Random ids, blank values and icon-only buttons are left out.
        self.assertEqual(
            report.splitlines()[2:],
            [
                '<input name="login-id" type="text">',
                '<button type="submit">Continue</button>',
                "<label>Login ID</label>",
            ],
        )
        self.assertIn("Wrote 3 fields to", output)
        dismiss.assert_called_once_with(self.page, timeout_ms=5000)

        # Both files are owner-only (the screenshot can show the username).
        self.assertEqual((self.dir / "login_page.png").read_bytes(), b"png")
        for name in ("login_page_fields.txt", "login_page.png"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600)

    def test_still_reports_when_the_page_is_slow_or_changed(self, _dismiss):
        self.page.wait_for_load_state.side_effect = PlaywrightTimeoutError("busy")
        self.page.get_by_label.return_value.wait_for.side_effect = (
            PlaywrightTimeoutError("gone")
        )
        output = self._discover()
        self.assertIn("still loading; continuing anyway", output)
        self.assertIn("login form didn't appear", output)
        report = (self.dir / "login_page_fields.txt").read_text()
        self.assertIn('name="login-id"', report)


if __name__ == "__main__":
    unittest.main()
