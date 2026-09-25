"""pam_cli.reachability with the network and sleeps mocked."""

import http.client
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from pam_cli import reachability

URL = "https://pam.example.com"
_SSL_ERROR = ssl.SSLCertVerificationError(
    1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed"
)
_SSL_ERROR.verify_message = "self-signed certificate"  # set by ssl on real errors
_CERT_ERROR = urllib.error.URLError(_SSL_ERROR)


class ProbeTests(unittest.TestCase):
    def _probe(self, error=None, url=URL):
        with patch("pam_cli.reachability.urllib.request.urlopen", side_effect=error):
            return reachability._probe(url)

    def test_reachable_answers(self):
        http_error = urllib.error.HTTPError(URL, 503, "down", {}, None)
        bad_reply = http.client.BadStatusLine("garbage")
        for name, error in (
            ("ok", None),
            ("http error", http_error),
            ("malformed reply", bad_reply),
        ):
            with self.subTest(name):
                self.assertIsNone(self._probe(error))

    def test_cert_verify_failure_reports_a_readable_problem(self):
        reason = self._probe(_CERT_ERROR)
        # The short reason, not the "<urlopen error ...>" wrapper.
        self.assertEqual(
            reason, "TLS certificate could not be verified: self-signed certificate"
        )
        self.assertEqual(self._probe(_SSL_ERROR), reason)  # also when unwrapped
        self.assertTrue(reachability.is_cert_problem(reason))
        self.assertFalse(reachability.is_cert_problem("connection refused"))

    def test_unreachable_returns_reason(self):
        for error, reason in (
            (ConnectionRefusedError("connection refused"), "connection refused"),
            (urllib.error.URLError("dns failure"), "dns failure"),
            (TimeoutError("timed out"), "timed out"),
        ):
            with self.subTest(reason):
                self.assertIn(reason, self._probe(error))
        self.assertIn("unknown url type", self._probe(url="not a url"))


@patch("pam_cli.reachability.time.sleep")
class CheckReachableTests(unittest.TestCase):
    def test_retries_then_succeeds(self, sleep):
        with patch("pam_cli.reachability._probe", side_effect=["down", None]) as probe:
            self.assertIsNone(reachability.check_reachable(URL))
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(reachability.RETRY_DELAY_S)

    def test_gives_up_with_the_last_reason_without_a_final_sleep(self, sleep):
        with patch("pam_cli.reachability._probe", return_value="refused") as probe:
            self.assertEqual(reachability.check_reachable(URL), "refused")
        self.assertEqual(probe.call_count, reachability.RETRY_ATTEMPTS)
        self.assertEqual(sleep.call_count, reachability.RETRY_ATTEMPTS - 1)

    def test_certificate_problem_skips_retries(self, sleep):
        with patch(
            "pam_cli.reachability._probe",
            return_value=f"{reachability._CERT_PROBLEM} (self-signed)",
        ) as probe:
            result = reachability.check_reachable(URL)
        self.assertTrue(result.startswith(reachability._CERT_PROBLEM))
        self.assertEqual(probe.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
