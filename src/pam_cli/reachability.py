"""Checks that the portal answers before the browser is started."""

import http.client
import logging
import ssl
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


TIMEOUT_S = 5.0
# A VPN tunnel may still be coming up when pamcli starts.
RETRY_ATTEMPTS = 3
RETRY_DELAY_S = 3.0

# Prefix of the reason for a TLS certificate problem. Such problems are
# deterministic, so check_reachable doesn't retry them.
_CERT_PROBLEM = "TLS certificate could not be verified"


def _is_cert_error(exc: BaseException) -> bool:
    # The certificate error may be wrapped in a URLError, so check both.
    return any(
        isinstance(e, ssl.SSLCertVerificationError)
        or "certificate verify failed" in str(e).lower()
        for e in (exc, getattr(exc, "reason", None))
        if e is not None
    )


def _probe(url: str) -> str | None:
    """Return None if the portal answers, else the reason it didn't.

    A TLS certificate that can't be verified is treated as a failure, rather
    than trusting a server whose identity couldn't be checked."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S):
            pass
    except (urllib.error.HTTPError, http.client.HTTPException):
        pass  # an HTTP error status, or even a malformed reply, is an answer
    except (OSError, ValueError) as exc:  # includes URLError and SSLError
        if not _is_cert_error(exc):
            return str(exc)
        # Unwrap a URLError; SSL errors carry a short reason in verify_message.
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return f"{_CERT_PROBLEM}: {getattr(cause, 'verify_message', None) or cause}"
    return None


def is_cert_problem(problem: str) -> bool:
    """True if a problem from check_reachable is a TLS certificate failure."""
    return problem.startswith(_CERT_PROBLEM)


def check_reachable(url: str) -> str | None:
    """Return None if the portal answers, else why it didn't. Retries a few
    times, since a VPN may still be connecting. A TLS certificate problem
    isn't retried: it's deterministic and won't clear on its own."""
    logger.info("Checking the portal is reachable: %s", url)
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        problem = _probe(url)
        if problem is None:
            return None
        if is_cert_problem(problem):
            return problem
        if attempt < RETRY_ATTEMPTS:
            logger.info(
                "Not reachable yet (attempt %d/%d: %s), retrying in %gs",
                attempt,
                RETRY_ATTEMPTS,
                problem,
                RETRY_DELAY_S,
            )
            time.sleep(RETRY_DELAY_S)
    return problem
