"""pamcli's log lines, written to stderr, look like this:

    [2026-09-24T07:35:20Z INFO  pamcli::accounts] Requesting access 1/2: ...

The time is UTC. Only pamcli's own messages use this format; messages
from Playwright and other libraries keep theirs."""

import logging
import re
from datetime import datetime, timezone

_PACKAGE = "pam_cli"
# WARNING is shortened so every level fits the 5-wide column.
_LEVEL_NAMES = {"WARNING": "WARN"}

# Control characters (except newline and tab), invisible direction marks,
# bidirectional overrides and line/paragraph separators. A page's text
# could use them to rewrite, reorder or hide what the terminal shows.
_UNSAFE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029"
    r"\u202a-\u202e\u2066-\u2069]"
)


def printable(text: str) -> str:
    """Text safe to show in a terminal: unsafe characters are written out
    as escapes (e.g. \\x1b or \\r) instead of taking effect."""
    return _UNSAFE.sub(
        lambda m: m.group().encode("unicode_escape").decode("ascii"), text
    )


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level = _LEVEL_NAMES.get(record.levelname, record.levelname)
        target = record.name.replace(_PACKAGE, "pamcli", 1).replace(".", "::")
        created = datetime.fromtimestamp(record.created, timezone.utc)
        timestamp = created.strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{timestamp} {level:<5} {target}] {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        # Messages can carry the portal's text, e.g. account names.
        return printable(line)


def setup_logging(verbose: bool = False) -> None:
    """Send pamcli's logs to stderr: INFO by default, DEBUG with -v."""
    handler = logging.StreamHandler()
    handler.setFormatter(_Formatter())
    logger = logging.getLogger(_PACKAGE)
    logger.handlers[:] = [handler]
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
