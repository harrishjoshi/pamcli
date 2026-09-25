"""Saving PAM_URL and PAM_USERNAME in the shell's start-up file (~/.zshrc or
~/.bashrc), so they're set in every new terminal. Offered by `pamcli setup`."""

import os
import re
import shlex
import sys
from pathlib import Path

from pam_cli.auth import check_url

# A variable set in a start-up file, e.g. `export PAM_URL=…` or `PAM_URL=…`.
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(\w+)=", re.MULTILINE)
_NAMES = ("PAM_URL", "PAM_USERNAME")


def rc_path() -> Path | None:
    """The start-up file of the user's default shell, or None if it isn't
    zsh or bash (or on Windows, where settings are made with setx)."""
    if sys.platform == "win32":
        return None
    shell = Path(os.environ.get("SHELL", "")).name
    home = Path.home()
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
    if shell == "bash":
        # macOS opens login shells, which read ~/.bash_profile, not ~/.bashrc.
        return home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    return None


def save(path: Path, settings: dict[str, str]) -> None:
    """Append the settings as `export` lines to the end of the file, which is
    created if it doesn't exist. Nothing already in the file is changed."""
    existing = path.read_bytes() if path.exists() else b""
    # A blank line before pamcli's lines, unless the file is empty.
    separator = "" if not existing else "\n" if existing.endswith(b"\n") else "\n\n"
    lines = [f"export {name}={shlex.quote(value)}" for name, value in settings.items()]
    with path.open("a", encoding="utf-8") as file:
        file.write(separator + "\n".join(["# pamcli settings", *lines]) + "\n")


def _shown(path: Path) -> str:
    """The path as the user would type it, e.g. ~/.zshrc."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _ask_url() -> str | None:
    """Ask until a valid login-page URL is entered; None if skipped."""
    while True:
        url = input("PAM_URL (the portal's login page; Enter to skip): ").strip()
        if not url:
            return None
        try:
            return check_url(url)
        except ValueError as exc:
            print(exc, file=sys.stderr)


def _missing(path: Path | None) -> list[str]:
    """The settings that aren't set yet: neither in the environment nor in
    the start-up file (which may not be loaded in this terminal yet)."""
    text = (
        path.read_text(encoding="utf-8", errors="replace")
        if path and path.exists()
        else ""
    )
    in_file = set(_ASSIGNMENT.findall(text))
    return [
        name
        for name in _NAMES
        if not os.environ.get(name, "").strip() and name not in in_file
    ]


def offer_to_save() -> None:
    """Offer to save whichever of PAM_URL and PAM_USERNAME aren't set yet in
    the shell's start-up file, and do it if the answer is yes. Anything else,
    or nothing missing, skips it."""
    if not sys.stdin.isatty():
        return
    path = rc_path()
    missing = _missing(path)
    if not missing:
        print("PAM_URL and PAM_USERNAME are already set.")
        return
    if path is None:
        print("To keep PAM_URL and PAM_USERNAME set, see Set up in the README.")
        return
    shown = _shown(path)
    try:
        answer = input(f"Save {' and '.join(missing)} in {shown}? [y/N] ")
    except EOFError:
        return
    if answer.strip().lower() not in ("y", "yes"):
        return
    settings = {}
    if "PAM_URL" in missing and (url := _ask_url()):
        settings["PAM_URL"] = url
    if "PAM_USERNAME" in missing and (
        username := input("PAM_USERNAME (Enter to skip): ").strip()
    ):
        settings["PAM_USERNAME"] = username
    if not settings:
        print("Nothing saved.")
        return
    save(path, settings)
    print(
        f"Saved {' and '.join(settings)} in {shown}. "
        f"Open a new terminal, or run: source {shown}"
    )
