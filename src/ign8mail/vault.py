"""Ansible Vault integration — vault password is read from IGN8MAIL_TOKEN."""

import os
from pathlib import Path
from typing import Any


def _password() -> str:
    token = os.environ.get("IGN8MAIL_TOKEN", "")
    if not token:
        raise RuntimeError(
            "IGN8MAIL_TOKEN is not set. "
            "Export this environment variable to your vault password before running ign8mail."
        )
    return token


def load(vault_path: Path) -> dict[str, Any]:
    """Decrypt and return vault contents as a dict."""
    from ansible_vault import Vault  # type: ignore[import-untyped]

    result = Vault(_password()).load(vault_path.read_text())
    return result if isinstance(result, dict) else {}


def save(data: dict[str, Any], vault_path: Path) -> None:
    """Encrypt data and write to vault_path (creates parent dirs as needed)."""
    from ansible_vault import Vault  # type: ignore[import-untyped]

    vault_path.parent.mkdir(parents=True, exist_ok=True)
    with vault_path.open("w") as fh:
        Vault(_password()).dump(data, fh)


def inject_env(vault_path: Path) -> None:
    """Decrypt vault and export IGN8_* entries as env vars (skips already-set vars)."""
    data = load(vault_path)
    for key, value in data.items():
        if isinstance(key, str) and key.startswith("IGN8_") and isinstance(value, str):
            if key not in os.environ:
                os.environ[key] = value


def get_mailbox_password(username: str, vault_path: Path) -> str:
    """Return the password for *username*, generating and saving one if absent."""
    import secrets

    data = load(vault_path) if vault_path.exists() else {}
    pw = (data.get("mailbox_passwords") or {}).get(username)
    if not pw:
        pw = secrets.token_hex(12)
        data.setdefault("mailbox_passwords", {})[username] = pw
        save(data, vault_path)
    return str(pw)


# ---- Cooperative webmail: users + personal/shared address model -----------
#
# A "user" is a Linux/IMAP account that can log in to the webmail. Each user
# owns one or more *personal* addresses (default: <username>@<domain>) that
# only they can read and send from. Every other mailbox in `mailbox_passwords`
# is *shared* — visible to all registered users via the webmail.


def list_users(data: dict[str, Any]) -> list[str]:
    """Return registered webmail usernames (Linux accounts)."""
    return sorted((data.get("users") or {}).keys())


def personal_addresses(data: dict[str, Any], username: str) -> list[str]:
    user = (data.get("users") or {}).get(username) or {}
    return list(user.get("personal_addresses") or [])


def all_personal_addresses(data: dict[str, Any]) -> set[str]:
    addrs: set[str] = set()
    for u in (data.get("users") or {}).values():
        addrs.update(u.get("personal_addresses") or [])
    return addrs


def address_owner(data: dict[str, Any], address: str) -> str | None:
    """Return the user that owns *address* personally, or None if shared/unknown."""
    for username, u in (data.get("users") or {}).items():
        if address in (u.get("personal_addresses") or []):
            return str(username)
    return None


def shared_local_parts(data: dict[str, Any]) -> list[str]:
    """Mailbox local-parts that are NOT in anyone's personal_addresses."""
    personal_locals = {addr.split("@", 1)[0] for addr in all_personal_addresses(data)}
    mailboxes = (data.get("mailbox_passwords") or {}).keys()
    return sorted(m for m in mailboxes if m not in personal_locals)


def is_shared_local(data: dict[str, Any], local_part: str) -> bool:
    personal_locals = {addr.split("@", 1)[0] for addr in all_personal_addresses(data)}
    return local_part in (data.get("mailbox_passwords") or {}) and local_part not in personal_locals


def visible_local_parts(data: dict[str, Any], username: str) -> list[str]:
    """Mailboxes the webmail should show to *username*: own personal + all shared."""
    own = {addr.split("@", 1)[0] for addr in personal_addresses(data, username)}
    return sorted(own | set(shared_local_parts(data)))


def allowed_senders(data: dict[str, Any], username: str, domain: str) -> list[str]:
    """Addresses *username* is allowed to use in From: (own personal + all shared)."""
    own = list(personal_addresses(data, username))
    shared = [f"{lp}@{domain}" for lp in shared_local_parts(data)]
    return sorted(set(own + shared))


def add_user(
    data: dict[str, Any], username: str, personal_addresses_list: list[str]
) -> dict[str, Any]:
    """Register *username* with the given personal addresses. Returns *data*."""
    data.setdefault("users", {})[username] = {
        "personal_addresses": sorted(set(personal_addresses_list))
    }
    return data


def sender_login_map(data: dict[str, Any], domain: str) -> dict[str, list[str]]:
    """Build {address: [allowed-SASL-users]} for Postfix smtpd_sender_login_maps."""
    users = list_users(data)
    result: dict[str, list[str]] = {}
    for u in users:
        for addr in personal_addresses(data, u):
            result.setdefault(addr, []).append(u)
    for lp in shared_local_parts(data):
        result.setdefault(f"{lp}@{domain}", []).extend(users)
    return {addr: sorted(set(allowed)) for addr, allowed in result.items() if allowed}
