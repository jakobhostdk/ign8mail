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
    """Return the plaintext password for *username* from vault mailbox_passwords."""
    data = load(vault_path)
    pw = (data.get("mailbox_passwords") or {}).get(username)
    if not pw:
        raise RuntimeError(
            f"No password found for '{username}' in vault.\n"
            f"Add it under  mailbox_passwords.{username}  in {vault_path}."
        )
    return str(pw)
