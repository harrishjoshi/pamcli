"""Environment files: the servers (IPs) to request together, with
optional hours, reason and account for all or some of them."""

import importlib.resources
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pam_cli.config import MAX_SESSION_HOURS
from pam_cli.paths import config_dir


def _default_environments_dir() -> Path:
    """PAM_ENV_DIR if set, else `environments` in the config folder."""
    override = os.environ.get("PAM_ENV_DIR")
    if override:
        return Path(override).expanduser()
    return config_dir() / "environments"


ENVIRONMENTS_DIR = _default_environments_dir()

# Letters, digits, _ and -, with dots between parts: can't form a path.
_ENV_NAME = re.compile(r"[\w-]+(\.[\w-]+)*")
_OPTIONAL_KEYS = {"hours", "reason", "account"}


@dataclass
class IPTarget:
    ip: str
    hours: int | None = None
    reason: str | None = None
    account: str | None = None


@dataclass
class EnvironmentConfig:
    targets: list[IPTarget]
    hours: int | None
    reason: str | None
    account: str | None


def _optional_fields(
    data: dict[str, Any], where: str, required: set[str]
) -> dict[str, Any]:
    """Read and check hours, reason and account from a JSON object.

    Unknown keys are errors, so a typo like "hour" isn't silently ignored."""
    unknown = data.keys() - _OPTIONAL_KEYS - required
    if unknown:
        raise RuntimeError(
            f"{where}: unknown key(s) {', '.join(sorted(map(repr, unknown)))}; "
            f"expected {', '.join(sorted(_OPTIONAL_KEYS | required))}."
        )
    hours, reason, account = data.get("hours"), data.get("reason"), data.get("account")
    if hours is not None and (
        not isinstance(hours, int)
        or isinstance(hours, bool)
        or not 1 <= hours <= MAX_SESSION_HOURS
    ):
        raise RuntimeError(
            f'{where}: "hours" must be an integer between 1 and '
            f"{MAX_SESSION_HOURS}, got {hours!r}."
        )
    for key, value in (("reason", reason), ("account", account)):
        if value is None:
            continue
        if not isinstance(value, str):
            raise RuntimeError(f'{where}: "{key}" must be a string.')
        if not value.strip():
            raise RuntimeError(f'{where}: "{key}" must not be blank.')
    return {"hours": hours, "reason": reason, "account": account}


def check_ip(value: str) -> str:
    """Return the value if it's a plain IPv4 address, else raise ValueError.

    The grid's Quick filter matches partial text, so anything less than a
    full address (e.g. 10.0.0 or a host name) could pick another server."""
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError(f"{value!r} is not an IPv4 address (e.g. 10.0.0.1).") from None
    return value


def _checked_ip(ip: str, path: Path) -> str:
    try:
        return check_ip(ip)
    except ValueError as exc:
        raise RuntimeError(f"{path}: {exc}") from None


def _parse_ip_entry(entry: object, path: Path) -> IPTarget:
    if isinstance(entry, str) and entry:
        return IPTarget(ip=_checked_ip(entry, path))
    if isinstance(entry, dict):
        ip = entry.get("ip")
        if not isinstance(ip, str) or not ip:
            raise RuntimeError(
                f'{path}: each entry in "ips" must have a non-empty string "ip".'
            )
        _checked_ip(ip, path)
        return IPTarget(ip=ip, **_optional_fields(entry, f"{path} ({ip})", {"ip"}))
    raise RuntimeError(
        f'{path}: each entry in "ips" must be a non-empty string or an object '
        'with an "ip" field.'
    )


def parse_ip_list(raw: str) -> list[IPTarget]:
    """Split a comma-separated list of IPs, dropping blanks and repeats.
    Raises ValueError if any of them isn't an IPv4 address."""
    ips = dict.fromkeys(ip.strip() for ip in raw.split(","))
    return [IPTarget(ip=check_ip(ip)) for ip in ips if ip]


def load_environment(label: str) -> EnvironmentConfig:
    """Load and check the environment file <label>.json (e.g. UAT or
    UAT.abc). Raises RuntimeError with a clear message on any problem."""
    if not _ENV_NAME.fullmatch(label):
        raise RuntimeError(
            f"Invalid environment name {label!r} — use NAME or NAME.PROJECT "
            "(letters, digits, _ and -)."
        )
    path = ENVIRONMENTS_DIR / f"{label}.json"
    if not path.exists():
        example = importlib.resources.files("pam_cli") / "environments" / "example.json"
        available = sorted(p.stem for p in ENVIRONMENTS_DIR.glob("*.json"))
        found = (
            f"Available: {', '.join(available)}."
            if available
            else "No environment files there yet."
        )
        raise RuntimeError(
            f"Environment {label!r} not found — no file at {path}. {found} "
            f"Start from {example}."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f'{path} must contain a JSON object with an "ips" list.')
    raw_ips = data.get("ips")
    if not isinstance(raw_ips, list) or not raw_ips:
        raise RuntimeError(f'{path} must have a non-empty "ips" list.')

    return EnvironmentConfig(
        targets=[_parse_ip_entry(entry, path) for entry in raw_ips],
        **_optional_fields(data, str(path), {"ips"}),
    )
