"""Configure a Thunderbird account for the provisioned mail server."""

import configparser
import platform
import re
from pathlib import Path


def find_profile() -> Path | None:
    """Return the default Thunderbird profile directory, or None if not found."""
    system = platform.system()
    if system == "Darwin":
        tb_root = Path.home() / "Library" / "Thunderbird"
    elif system == "Linux":
        tb_root = Path.home() / ".thunderbird"
    elif system == "Windows":
        import os
        tb_root = Path(os.environ.get("APPDATA", "")) / "Thunderbird"
    else:
        return None

    ini_path = tb_root / "profiles.ini"
    if ini_path.exists():
        cfg = configparser.ConfigParser()
        cfg.read(ini_path)
        for section in cfg.sections():
            if cfg.get(section, "Default", fallback="0") == "1":
                raw = cfg.get(section, "Path", fallback="")
                if raw:
                    is_relative = cfg.getint(section, "IsRelative", fallback=1)
                    p = (tb_root / raw) if is_relative else Path(raw)
                    if p.is_dir():
                        return p

    profiles_dir = tb_root / "Profiles"
    candidates = list(profiles_dir.glob("*.default-release")) + list(profiles_dir.glob("*.default")) if profiles_dir.is_dir() else []
    if candidates:
        return candidates[0]

    return None


def _next_n(text: str, prefix: str) -> int:
    ids = [int(m.group(1)) for m in re.finditer(rf'"{re.escape(prefix)}(\d+)\.', text)]
    return max(ids, default=0) + 1


def _get_str_pref(text: str, key: str) -> str | None:
    m = re.search(rf'user_pref\("{re.escape(key)}", "([^"]*)"\)', text)
    return m.group(1) if m else None


def _set_str_pref(text: str, key: str, value: str) -> str:
    replacement = f'user_pref("{key}", "{value}");'
    pattern = re.compile(rf'user_pref\("{re.escape(key)}"[^)]*\);')
    if pattern.search(text):
        return pattern.sub(replacement, text)
    return text + f"\n{replacement}"


def configure_account(profile_dir: Path, email: str, display_name: str, mail_host: str) -> None:
    """Write Thunderbird prefs for an IMAP/SMTP account. Raises if prefs.js is missing."""
    prefs_path = profile_dir / "prefs.js"
    text = prefs_path.read_text(encoding="utf-8") if prefs_path.exists() else ""

    acct_n = _next_n(text, "mail.account.account")
    srv_n = _next_n(text, "mail.server.server")
    id_n = _next_n(text, "mail.identity.id")
    smtp_n = _next_n(text, "mail.smtpserver.smtp")

    acct = f"account{acct_n}"
    srv = f"server{srv_n}"
    ident = f"id{id_n}"
    smtp = f"smtp{smtp_n}"

    old_accounts = _get_str_pref(text, "mail.accountmanager.accounts") or ""
    new_accounts = f"{old_accounts},{acct}".lstrip(",")

    old_smtp_list = _get_str_pref(text, "mail.smtpservers") or ""
    new_smtp_list = f"{old_smtp_list},{smtp}".lstrip(",")

    text = _set_str_pref(text, "mail.accountmanager.accounts", new_accounts)
    text = _set_str_pref(text, "mail.smtpservers", new_smtp_list)

    if not _get_str_pref(text, "mail.accountmanager.defaultaccount"):
        text = _set_str_pref(text, "mail.accountmanager.defaultaccount", acct)
    if not _get_str_pref(text, "mail.smtp.defaultserver"):
        text = _set_str_pref(text, "mail.smtp.defaultserver", smtp)

    block = f"""
// ign8mail: {email}
user_pref("mail.account.{acct}.identities", "{ident}");
user_pref("mail.account.{acct}.server", "{srv}");
user_pref("mail.identity.{ident}.fullName", "{display_name}");
user_pref("mail.identity.{ident}.smtpServer", "{smtp}");
user_pref("mail.identity.{ident}.useremail", "{email}");
user_pref("mail.server.{srv}.hostname", "{mail_host}");
user_pref("mail.server.{srv}.name", "{mail_host}");
user_pref("mail.server.{srv}.port", 993);
user_pref("mail.server.{srv}.socketType", 3);
user_pref("mail.server.{srv}.type", "imap");
user_pref("mail.server.{srv}.userName", "{email}");
user_pref("mail.smtpserver.{smtp}.authMethod", 3);
user_pref("mail.smtpserver.{smtp}.hostname", "{mail_host}");
user_pref("mail.smtpserver.{smtp}.port", 587);
user_pref("mail.smtpserver.{smtp}.socketType", 2);
user_pref("mail.smtpserver.{smtp}.username", "{email}");
"""
    prefs_path.write_text(text + block, encoding="utf-8")
