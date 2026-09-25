"""pam_cli.polling.poll_until with the clock mocked."""

import unittest
from unittest.mock import patch

from pam_cli.polling import poll_until


class PollUntilTests(unittest.TestCase):
    def test_returns_first_non_none_result_including_falsy(self):
        answers = iter([None, None, 0])  # 0 (an empty grid) is an answer
        with (
            patch("pam_cli.polling.time.monotonic", return_value=0),
            patch("pam_cli.polling.time.sleep") as sleep,
        ):
            self.assertEqual(poll_until(lambda: next(answers), 1000, 0.1), 0)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.1)

    def test_returns_none_after_timeout(self):
        with (
            patch("pam_cli.polling.time.monotonic", side_effect=[0, 0.5, 2]),
            patch("pam_cli.polling.time.sleep"),
        ):
            self.assertIsNone(poll_until(lambda: None, 1000))


if __name__ == "__main__":
    unittest.main()
