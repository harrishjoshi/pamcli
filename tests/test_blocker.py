"""pam_cli.blocker: the overlay is installed and removed as expected."""

import unittest
from unittest.mock import MagicMock

from playwright.sync_api import Error as PlaywrightError

from pam_cli.blocker import BLOCKER_ID, install_input_blocker, remove_input_blocker


class BlockerTests(unittest.TestCase):
    def test_install_input_blocker_injects_the_overlay(self):
        page = MagicMock()
        install_input_blocker(page)

        page.evaluate.assert_called_once()
        script, element_id = page.evaluate.call_args.args
        self.assertEqual(element_id, BLOCKER_ID)
        self.assertIn("document.getElementById(id)", script)

    def test_remove_input_blocker_deletes_the_overlay(self):
        page = MagicMock()
        remove_input_blocker(page)

        page.evaluate.assert_called_once()
        script, element_id = page.evaluate.call_args.args
        self.assertEqual(element_id, BLOCKER_ID)
        self.assertIn("?.remove()", script)

    def test_removing_ignores_an_already_closed_page(self):
        page = MagicMock()
        page.evaluate.side_effect = PlaywrightError("Page is closed")

        remove_input_blocker(page)  # must not raise


if __name__ == "__main__":
    unittest.main()
