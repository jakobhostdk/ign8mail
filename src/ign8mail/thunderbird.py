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
        import os as _os
        tb_root = Path(_os.environ.get("APPDATA", "")) / "Thunderbird"
    else:
        return None

    ini_path = tb_root / "profiles.ini"
    if ini_path.exists():
        cfg = configparser.ConfigParser()
        cfg.read(ini_path)

        # Modern Thunderbird: [Install*] section holds the actually-launched profile.
        for section in cfg.sections():
            if section.startswith("Install"):
                raw = cfg.get(section, "Default", fallback="")
                if raw:
                    p = (tb_root / raw) if not Path(raw).is_absolute() else Path(raw)
                    if p.is_dir():
                        return p

        # Legacy fallback: Profile section with Default=1
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


def find_accounts(profile_dir: Path) -> set[str]:
    """Return the set of email addresses already configured in Thunderbird."""
    prefs_path = profile_dir / "prefs.js"
    if not prefs_path.exists():
        return set()
    text = prefs_path.read_text(encoding="utf-8")
    return set(re.findall(r'user_pref\("mail\.identity\.id\d+\.useremail", "([^"]*)"\)', text))


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

    local_user = display_name  # IMAP/SMTP server expects the local username, not full email
    block = f"""
// ign8mail: {email}
user_pref("mail.account.{acct}.identities", "{ident}");
user_pref("mail.account.{acct}.server", "{srv}");
user_pref("mail.identity.{ident}.fullName", "{display_name}");
user_pref("mail.identity.{ident}.smtpServer", "{smtp}");
user_pref("mail.identity.{ident}.useremail", "{email}");
user_pref("mail.server.{srv}.hostname", "{mail_host}");
user_pref("mail.server.{srv}.name", "{email}");
user_pref("mail.server.{srv}.authMethod", 3);
user_pref("mail.server.{srv}.port", 993);
user_pref("mail.server.{srv}.socketType", 3);
user_pref("mail.server.{srv}.type", "imap");
user_pref("mail.server.{srv}.userName", "{local_user}");
user_pref("mail.smtpserver.{smtp}.authMethod", 3);
user_pref("mail.smtpserver.{smtp}.hostname", "{mail_host}");
user_pref("mail.smtpserver.{smtp}.port", 587);
user_pref("mail.smtpserver.{smtp}.socketType", 2);
user_pref("mail.smtpserver.{smtp}.username", "{local_user}");
"""
    prefs_path.write_text(text + block, encoding="utf-8")


def fix_usernames(profile_dir: Path, domain: str) -> int:
    """Replace full-email userName values with local username and ensure authMethod=3 for all accounts on domain.

    Returns the number of prefs lines changed.
    """
    prefs_path = profile_dir / "prefs.js"
    if not prefs_path.exists():
        return 0
    text = prefs_path.read_text(encoding="utf-8")
    new_text = re.sub(
        r'(user_pref\("mail\.server\.[^"]+\.userName",\s*")([^@"]+)@' + re.escape(domain) + r'(")',
        lambda m: m.group(1) + m.group(2) + m.group(3),
        text,
    )
    new_text = re.sub(
        r'(user_pref\("mail\.smtpserver\.[^"]+\.username",\s*")([^@"]+)@' + re.escape(domain) + r'(")',
        lambda m: m.group(1) + m.group(2) + m.group(3),
        new_text,
    )

    # Find all IMAP server IDs that serve this domain (by matching hostname)
    imap_servers = set(re.findall(
        r'user_pref\("mail\.server\.(server\d+)\.hostname",\s*"mail\.' + re.escape(domain) + r'"\)',
        new_text,
    ))
    for srv in imap_servers:
        if not re.search(rf'user_pref\("mail\.server\.{re.escape(srv)}\.authMethod"', new_text):
            # Inject authMethod=3 right after the hostname line
            new_text = re.sub(
                rf'(user_pref\("mail\.server\.{re.escape(srv)}\.hostname"[^\n]+\n)',
                lambda m: m.group(1) + f'user_pref("mail.server.{srv}.authMethod", 3);\n',
                new_text,
            )

    # Same for SMTP servers
    smtp_servers = set(re.findall(
        r'user_pref\("mail\.smtpserver\.(smtp\d+)\.hostname",\s*"mail\.' + re.escape(domain) + r'"\)',
        new_text,
    ))
    for smtp in smtp_servers:
        if not re.search(rf'user_pref\("mail\.smtpserver\.{re.escape(smtp)}\.authMethod"', new_text):
            new_text = re.sub(
                rf'(user_pref\("mail\.smtpserver\.{re.escape(smtp)}\.hostname"[^\n]+\n)',
                lambda m: m.group(1) + f'user_pref("mail.smtpserver.{smtp}.authMethod", 3);\n',
                new_text,
            )

    changed = sum(a != b for a, b in zip(text.splitlines(), new_text.splitlines()))
    # Account for added lines (zip stops at shortest)
    changed += abs(len(new_text.splitlines()) - len(text.splitlines()))
    if new_text != text:
        prefs_path.write_text(new_text, encoding="utf-8")
    return changed


def remove_account(profile_dir: Path, email: str) -> bool:
    """Remove all Thunderbird prefs for the given email address. Returns True if found."""
    prefs_path = profile_dir / "prefs.js"
    if not prefs_path.exists():
        return False
    text = prefs_path.read_text(encoding="utf-8")

    m = re.search(rf'user_pref\("mail\.identity\.(id\d+)\.useremail", "{re.escape(email)}"\)', text)
    if not m:
        return False
    ident = m.group(1)

    m2 = re.search(rf'user_pref\("mail\.account\.(account\d+)\.identities", "[^"]*{re.escape(ident)}[^"]*"\)', text)
    acct = m2.group(1) if m2 else None

    srv = None
    if acct:
        m3 = re.search(rf'user_pref\("mail\.account\.{re.escape(acct)}\.server", "([^"]*)"\)', text)
        srv = m3.group(1) if m3 else None

    m4 = re.search(rf'user_pref\("mail\.identity\.{re.escape(ident)}\.smtpServer", "([^"]*)"\)', text)
    smtp = m4.group(1) if m4 else None

    prefixes = [f'"mail.identity.{ident}.']
    if acct:
        prefixes.append(f'"mail.account.{acct}.')
    if srv:
        prefixes.append(f'"mail.server.{srv}.')
    if smtp:
        prefixes.append(f'"mail.smtpserver.{smtp}.')

    new_lines = [ln for ln in text.splitlines(keepends=True) if not any(p in ln for p in prefixes)]
    text = "".join(new_lines)

    if acct:
        old_accounts = _get_str_pref(text, "mail.accountmanager.accounts") or ""
        new_accounts = ",".join(a for a in old_accounts.split(",") if a and a != acct)
        text = _set_str_pref(text, "mail.accountmanager.accounts", new_accounts)
        if _get_str_pref(text, "mail.accountmanager.defaultaccount") == acct:
            remaining = [a for a in new_accounts.split(",") if a]
            text = _set_str_pref(text, "mail.accountmanager.defaultaccount", remaining[0] if remaining else "")

    if smtp:
        old_smtp = _get_str_pref(text, "mail.smtpservers") or ""
        new_smtp = ",".join(s for s in old_smtp.split(",") if s and s != smtp)
        text = _set_str_pref(text, "mail.smtpservers", new_smtp)
        if _get_str_pref(text, "mail.smtp.defaultserver") == smtp:
            remaining = [s for s in new_smtp.split(",") if s]
            text = _set_str_pref(text, "mail.smtp.defaultserver", remaining[0] if remaining else "")

    text = re.sub(rf"// ign8mail: {re.escape(email)}\n", "", text)
    prefs_path.write_text(text, encoding="utf-8")
    return True


def check_status(profile_dir: Path, email: str) -> dict:
    """Return a dict describing Thunderbird configuration status for an email address.

    Keys:
        configured – bool: account present in prefs.js
    """
    return {"configured": email in find_accounts(profile_dir)}


def generate_autoconfig_xml(domain: str, mail_host: str) -> str:
    """Return an ISPDB-style autoconfig XML string for the given domain.

    Thunderbird fetches this from https://autoconfig.<domain>/.well-known/autoconfig/mail/config-v1.1.xml
    and uses it to pre-fill server settings when the user adds a new account.
    The password is not included — Thunderbird prompts once and stores it in the OS keychain.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<clientConfig version="1.1">
  <emailProvider id="{domain}">
    <domain>{domain}</domain>
    <displayName>{domain} Mail</displayName>
    <incomingServer type="imap">
      <hostname>{mail_host}</hostname>
      <port>993</port>
      <socketType>SSL</socketType>
      <authentication>password-cleartext</authentication>
      <username>%EMAILLOCALPART%</username>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>{mail_host}</hostname>
      <port>587</port>
      <socketType>STARTTLS</socketType>
      <authentication>password-cleartext</authentication>
      <username>%EMAILLOCALPART%</username>
    </outgoingServer>
  </emailProvider>
</clientConfig>
"""
