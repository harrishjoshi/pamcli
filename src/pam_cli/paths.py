"""Where pamcli keeps its files, and how it writes private ones."""

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    """The user config folder on this system (environment files go in its
    `environments` folder)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "pamcli"


def write_private(path: Path, data: bytes) -> None:
    """Write a file only its owner can read (screenshots can show the
    username). Refuses to write through a symlink placed at that name."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as file:
        if hasattr(os, "fchmod"):
            os.fchmod(file.fileno(), 0o600)  # os.open's mode only applies to new files
        file.write(data)
