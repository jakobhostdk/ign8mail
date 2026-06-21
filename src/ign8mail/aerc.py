"""Configure aerc accounts for the provisioned mail server."""

import os
import re
from pathlib import Path


def find_config() -> Path | None:
    """Return the aerc config directory, or None if not found."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    p = xdg / "aerc"
    if p.is_dir():
        return p
    return None


def ensure_config() -> Path:
    """Return the aerc config directory, creating it if needed."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    p = xdg / "aerc"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _section_name(local_user: str, domain: str) -> str:
    """Return the accounts.conf section name for an account."""
    return local_user


def _url_encode_password(pw: str) -> str:
    """Percent-encode characters that are not safe in a URL userinfo."""
    safe = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=")
    return "".join(c if c in safe else f"%{ord(c):02X}" for c in pw)


def configure_accounts(
    config_dir: Path,
    domain: str,
    mail_host: str,
    credentials: dict[str, str],
    full_names: dict[str, str] | None = None,
) -> int:
    """Write or update accounts in aerc's accounts.conf.

    credentials: mapping of local_user → plaintext password.
    full_names:  optional mapping of local_user → display name.
    Returns the number of accounts written.
    """
    accounts_path = config_dir / "accounts.conf"
    existing = _parse_accounts(accounts_path) if accounts_path.exists() else {}

    for local_user, password in credentials.items():
        enc_pw = _url_encode_password(password)
        display = (full_names or {}).get(local_user, local_user.capitalize())
        email = f"{local_user}@{domain}"
        section = _section_name(local_user, domain)

        # Remove any legacy section name variants for this user (e.g. user@domain)
        for old_key in (f"{local_user}@{domain}",):
            existing.pop(old_key, None)

        existing[section] = {
            # aerc schemes: imaps = implicit TLS, smtp = STARTTLS, smtps = implicit TLS
            "source": f"imaps://{local_user}:{enc_pw}@{mail_host}:993",
            "outgoing": f"smtp+plain://{local_user}:{enc_pw}@{mail_host}:587",
            "from": f"{display} <{email}>",
            "default": "INBOX",
            "copy-to": "Sent",
        }

    _write_accounts(accounts_path, existing)
    accounts_path.chmod(0o600)
    return len(credentials)


def remove_account(config_dir: Path, local_user: str, domain: str) -> bool:
    """Remove an account section from accounts.conf. Returns True if it was present."""
    accounts_path = config_dir / "accounts.conf"
    if not accounts_path.exists():
        return False
    existing = _parse_accounts(accounts_path)
    section = _section_name(local_user, domain)
    if section not in existing:
        return False
    del existing[section]
    _write_accounts(accounts_path, existing)
    return True


def list_accounts(config_dir: Path) -> list[str]:
    """Return section names from accounts.conf."""
    accounts_path = config_dir / "accounts.conf"
    if not accounts_path.exists():
        return []
    return list(_parse_accounts(accounts_path).keys())


# ---------------------------------------------------------------------------
# Internal helpers — simple INI parser that preserves insertion order and
# does not add spaces around '=' (aerc requires 'key = value' format but
# the parser is lenient, so we use 'key = value' for readability).
# ---------------------------------------------------------------------------

def _parse_accounts(path: Path) -> dict[str, dict[str, str]]:
    """Parse accounts.conf into {section: {key: value}} preserving order."""
    result: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            result.setdefault(current, {})
        elif "=" in line and current is not None:
            key, _, val = line.partition("=")
            result[current][key.strip()] = val.strip()
    return result


def _write_accounts(path: Path, sections: dict[str, dict[str, str]]) -> None:
    """Write sections back to accounts.conf using aerc's key=value format."""
    lines: list[str] = []
    for section, kv in sections.items():
        lines.append(f"[{section}]")
        for key, val in kv.items():
            lines.append(f"{key}={val}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
