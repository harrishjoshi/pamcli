"""Everything that depends on the portal's pages, plus pamcli's defaults.

If the portal changes, this is the file to update; `pamcli discover`
shows the login page's current fields."""

# Labels/button text on the portal's login screens.
TEXT = {
    "accept_button": "Accept",
    "username_label": "Username",
    "password_label": "Password",
    "login_button": "Log In",
    "totp_heading": "TOTP - Enter Token",
    "totp_submit_button": "Submit",
}

# The TOTP field. Its id changes on every page load, so it's found by its
# name attribute.
TOTP_INPUT_SELECTOR = "input[name='twofaprompt']"

DEFAULT_TIMEOUT_MS = 30_000

# Grid rows and their account-name and Access cells. The Access cell isn't a
# real button, so it's found by its column id.
# ":visible" skips rows the Quick filter has hidden.
GRID_ROW_SELECTOR = "tr.grid__row:visible"
ACCOUNT_NAME_CELL_SELECTOR = "td[data-column-id='AccountName']"
ACCESS_CELL_SELECTOR = "td[data-column-id='AccessButton']"

# The Access dialog's Quick Launch tab. The dialog's other tabs have their
# own hidden Hours and Reason fields, so searches stay inside this one.
QUICK_LAUNCH_SCOPE = "ps-quick-launch"

# Shown in the Access dialog, instead of the Hours and Reason fields, when the
# account already has an active request. The notice also says until when
# that request is valid.
EXISTING_REQUEST_TEXT = "An existing active request for this account can be reused"

DEFAULT_SESSION_HOURS = 12
MAX_SESSION_HOURS = 12
