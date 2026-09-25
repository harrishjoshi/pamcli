"""An invisible layer over the page that stops clicks and typing from
reaching it while pamcli fills in the login form.

pamcli types into fields directly and presses buttons without a real
click, so the layer doesn't get in its way."""

import logging

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

logger = logging.getLogger(__name__)


BLOCKER_ID = "pamcli-input-blocker"

_INSTALL_JS = """id => {
  if (document.getElementById(id)) return;
  const el = document.createElement('div');
  el.id = id;
  el.tabIndex = 0;
  el.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;background:transparent;' +
    'cursor:not-allowed;';
  document.body.appendChild(el);
  el.focus({ preventScroll: true });
}"""

_REMOVE_JS = "id => document.getElementById(id)?.remove()"


def install_input_blocker(page: Page) -> None:
    """Cover the page so manual clicks and typing don't reach it."""
    page.evaluate(_INSTALL_JS, BLOCKER_ID)


def remove_input_blocker(page: Page) -> None:
    """Remove the layer. If the page has already closed, that's ignored so
    it doesn't hide the error being reported."""
    try:
        page.evaluate(_REMOVE_JS, BLOCKER_ID)
    except PlaywrightError as exc:
        logger.debug("Could not remove the input blocker: %s", exc)
