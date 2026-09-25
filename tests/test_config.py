"""pam_cli.config: the values are consistent with each other.

Deliberately not pinned to exact text: updating a label after a portal
change shouldn't need a test change."""

import unittest

from pam_cli import config


class ConfigTests(unittest.TestCase):
    def test_default_session_hours_are_within_the_maximum(self):
        self.assertTrue(1 <= config.DEFAULT_SESSION_HOURS <= config.MAX_SESSION_HOURS)

    def test_page_labels_and_selectors_are_set(self):
        selectors = {
            "TOTP_INPUT_SELECTOR": config.TOTP_INPUT_SELECTOR,
            "GRID_ROW_SELECTOR": config.GRID_ROW_SELECTOR,
            "ACCOUNT_NAME_CELL_SELECTOR": config.ACCOUNT_NAME_CELL_SELECTOR,
            "ACCESS_CELL_SELECTOR": config.ACCESS_CELL_SELECTOR,
            "QUICK_LAUNCH_SCOPE": config.QUICK_LAUNCH_SCOPE,
            "EXISTING_REQUEST_TEXT": config.EXISTING_REQUEST_TEXT,
        }
        for name, value in {**config.TEXT, **selectors}.items():
            with self.subTest(name):
                self.assertTrue(value.strip())


if __name__ == "__main__":
    unittest.main()
