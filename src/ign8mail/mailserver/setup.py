"""SSH into the provisioned server and install/configure the mail stack."""

import io
import json
import time
from pathlib import Path

import paramiko

from ign8mail.config import Config

_POLICY_DAEMON = '''\
#!/usr/bin/env python3
"""Postfix policy daemon: auto-creates mailboxes for unknown local recipients."""
import json
import os
import secrets
import socketserver
import subprocess
import threading
import time

SILENCED = "/etc/postfix/silenced_recipients"
DOMAIN_FILE = "/etc/postfix/ign8mail_domain"
LOG = "/var/log/ign8mail-autocreated.jsonl"
LOCAL_RECIPIENTS = "/etc/postfix/local_recipients"
_SUBDIRS = ("cur", "new", "tmp")
_FOLDERS = (".Sent", ".Drafts", ".Trash", ".Junk")
_lock = threading.Lock()


def _local_domain():
    if os.path.exists(DOMAIN_FILE):
        return open(DOMAIN_FILE).read().strip()
    r = subprocess.run(["postconf", "-h", "mydomain"], capture_output=True, text=True)
    return r.stdout.strip()


def _user_exists(username):
    return subprocess.run(["id", username], capture_output=True).returncode == 0


def _is_silenced(username):
    if not os.path.exists(SILENCED):
        return False
    with open(SILENCED) as f:
        return username in {line.strip() for line in f}


def _create_user(username):
    with _lock:
        if _user_exists(username):
            return
        password = secrets.token_hex(12)
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", username], check=True)
        proc = subprocess.Popen(["chpasswd"], stdin=subprocess.PIPE)
        proc.communicate(f"{username}:{password}\\n".encode())
        home = f"/home/{username}"
        for sub in _SUBDIRS:
            os.makedirs(f"{home}/Maildir/{sub}", exist_ok=True)
        for folder in _FOLDERS:
            for sub in _SUBDIRS:
                os.makedirs(f"{home}/Maildir/{folder}/{sub}", exist_ok=True)
        subprocess.run(["chown", "-R", f"{username}:{username}", f"{home}/Maildir"], check=True)
        with open(LOCAL_RECIPIENTS, "a") as f:
            f.write(f"{username} {username}\\n")
        subprocess.run(["postmap", LOCAL_RECIPIENTS], check=True)
        subprocess.run(
            ["doveadm", "mailbox", "create", "-u", username, "Drafts", "Sent", "Trash", "Junk"],
            capture_output=True,
        )
        with open(LOG, "a") as f:
            json.dump({"username": username, "password": password, "ts": int(time.time())}, f)
            f.write("\\n")


class _PolicyHandler(socketserver.StreamRequestHandler):
    def handle(self):
        attrs = {}
        for raw in self.rfile:
            line = raw.decode().strip()
            if not line:
                self.wfile.write(f"action={self._decide(attrs)}\\n\\n".encode())
                attrs = {}
            elif "=" in line:
                k, v = line.split("=", 1)
                attrs[k] = v

    def _decide(self, attrs):
        recipient = attrs.get("recipient", "")
        if not recipient or "@" not in recipient:
            return "DUNNO"
        local_part, domain_part = recipient.rsplit("@", 1)
        if domain_part.lower() != _local_domain().lower():
            return "DUNNO"
        local = local_part.lower()
        if _is_silenced(local):
            return "REJECT User unknown"
        if not _user_exists(local):
            try:
                _create_user(local)
            except Exception as exc:
                return f"DEFER_IF_PERMIT Service error: {exc}"
        return "DUNNO"


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", 10023), _PolicyHandler) as srv:
        srv.serve_forever()
'''

_POLICY_SERVICE = """\
[Unit]
Description=ign8mail Postfix autocreate policy daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/ign8mail-policy
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""

_LANDING_PAGE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ign8mail</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0d0f14;
      --surface: #161b25;
      --border: #1e2738;
      --accent: #3b82f6;
      --accent-dim: #1d4ed8;
      --text: #e2e8f0;
      --muted: #64748b;
      --flame: #f97316;
    }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 1rem;
      padding: 3rem 3.5rem;
      max-width: 520px;
      width: 100%;
      text-align: center;
    }
    .logo {
      font-size: 2.4rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      margin-bottom: 0.25rem;
    }
    .logo span { color: var(--flame); }
    .tagline {
      color: var(--muted);
      font-size: 0.8rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 2.5rem;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 0.6rem;
      background: rgba(59,130,246,.08);
      border: 1px solid rgba(59,130,246,.2);
      border-radius: 2rem;
      padding: 0.5rem 1.2rem;
      font-size: 0.85rem;
      color: var(--accent);
      margin-bottom: 2rem;
    }
    .dot {
      width: 7px; height: 7px;
      background: var(--accent);
      border-radius: 50%;
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.3; }
    }
    .desc {
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.8;
    }
    .desc code {
      color: var(--text);
      background: var(--border);
      border-radius: 4px;
      padding: 0.1em 0.4em;
      font-size: 0.9em;
    }
    .divider {
      border: none;
      border-top: 1px solid var(--border);
      margin: 2rem 0;
    }
    .proto {
      display: flex;
      justify-content: center;
      gap: 1.5rem;
      font-size: 0.75rem;
      color: var(--muted);
    }
    .proto span { display: flex; flex-direction: column; gap: 0.2rem; }
    .proto strong { color: var(--text); font-size: 0.8rem; }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">ign<span>8</span>mail</div>
    <div class="tagline">production mail infrastructure</div>
    <div class="status">
      <div class="dot"></div>
      under construction
    </div>
    <p class="desc">
      This server is being configured.<br>
      SMTP and IMAP services are <code>active</code>.
    </p>
    <hr class="divider">
    <div class="proto">
      <span><strong>SMTP</strong>port 587 · STARTTLS</span>
      <span><strong>IMAP</strong>port 993 · SSL/TLS</span>
    </div>
  </div>
</body>
</html>
"""

_WEBMAIL_WSGI = '''\
import json, secrets
from pathlib import Path
from django.conf import settings

_d = json.loads(Path('/etc/ign8mail/webmail.json').read_text())
if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY=_d.get('secret_key') or secrets.token_hex(32),
        ALLOWED_HOSTS=['*'],
        INSTALLED_APPS=['ign8mail.webmail'],
        MIDDLEWARE=[
            'django.middleware.security.SecurityMiddleware',
            'django.contrib.sessions.middleware.SessionMiddleware',
            'django.middleware.common.CommonMiddleware',
            'django.middleware.csrf.CsrfViewMiddleware',
        ],
        SESSION_ENGINE='django.contrib.sessions.backends.signed_cookies',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_TRUSTED_ORIGINS=['https://' + _d['domain'], 'https://webmail.' + _d['domain']],
        TEMPLATES=[{
            'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [], 'APP_DIRS': True,
            'OPTIONS': {'context_processors': ['django.template.context_processors.request']},
        }],
        ROOT_URLCONF='ign8mail.webmail.urls',
        WEBMAIL_DOMAIN=_d['domain'],
        WEBMAIL_IMAP_HOST=_d['imap_host'],
        WEBMAIL_SMTP_HOST=_d.get('smtp_host') or _d['imap_host'],
        WEBMAIL_PASSWORDS=_d['passwords'],
        WEBMAIL_AUTOCREATED=_d.get('autocreated', {}),
        WEBMAIL_USERS=_d.get('users', {}),
        WEBMAIL_WORK_DIR='/var/lib/ign8mail',
        WEBMAIL_VAULT_PATH='/etc/ign8mail/webmail.json',
        WEBMAIL_OVERLAY_DB='/var/lib/ign8mail/seen.db',
        WEBMAIL_SERVER_MODE=True,
    )
import django; django.setup()
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
'''

_WEBMAIL_SERVICE = """\
[Unit]
Description=ign8mail webmail interface
After=network.target

[Service]
Type=simple
ExecStart=/opt/ign8mail-webmail/venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8080 wsgi:application
WorkingDirectory=/opt/ign8mail-webmail
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

# Packages installed on the server
PACKAGES = "postfix postfix-mysql dovecot-core dovecot-imapd dovecot-pop3d rspamd certbot nginx redis-server"

_FOLDER_MAP = {
    "Inbox": "",
    "Sent": ".Sent",
    "Drafts": ".Drafts",
    "Trash": ".Trash",
    "Junk": ".Junk",
}

# Standard IMAP folders created via doveadm so Dovecot registers them properly.
_STANDARD_MAILBOXES = ("Drafts", "Sent", "Trash", "Junk")


def _find_project_root() -> Path:
    """Return the ign8mail project root by reading the package's install metadata."""
    import json
    from importlib.metadata import distribution

    dist = distribution("ign8mail")
    raw = dist.read_text("direct_url.json") or ""
    if raw:
        info = json.loads(raw)
        url = info.get("url", "")
        if url.startswith("file://"):
            root = Path(url[7:])
            if (root / "pyproject.toml").exists():
                return root
    # Fallback: walk up from this file (works for true editable installs)
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Cannot find ign8mail project root — is the package installed from source?")


def _maildir_dirs(home: str) -> str:
    """Space-separated list of all Maildir subdirs to create for a user."""
    base = f"{home}/Maildir"
    dirs = [f"{base}/{sub}" for sub in ("cur", "new", "tmp")]
    for remote_suffix in _FOLDER_MAP.values():
        if remote_suffix:
            dirs += [f"{base}/{remote_suffix}/{sub}" for sub in ("cur", "new", "tmp")]
    return " ".join(dirs)


class MailServerSetup:
    def __init__(self, cfg: Config, host: str, private_key_path: str) -> None:
        self._cfg = cfg
        self._host = host
        self._key_path = private_key_path
        self._ssh: paramiko.SSHClient | None = None
        # Stored one level above the keys/ subdirectory: .ign8mail/known_hosts
        self._known_hosts_path = Path(private_key_path).parent.parent / "known_hosts"

    def connect(self, retries: int = 10, delay: int = 15) -> None:
        self._drop_known_host()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        key = paramiko.RSAKey.from_private_key_file(self._key_path)

        for attempt in range(retries):
            try:
                client.connect(self._host, username="root", pkey=key, timeout=10)
                self._ssh = client
                self._save_known_host(client)
                return
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(delay)

    def disconnect(self) -> None:
        if self._ssh:
            self._ssh.close()
            self._ssh = None

    def run(self) -> None:
        assert self._ssh, "Call connect() first"
        self._exec("apt-get update -qq && apt-get upgrade -y -qq")
        self._exec(f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {PACKAGES}")
        self._configure_postfix()
        self._configure_dovecot()
        self._issue_cert()

    def backup(self) -> dict:
        """Collect server state (users + config) needed to restore after a rebuild."""
        assert self._ssh, "Call connect() first"
        passwd_out = self._exec(
            "getent passwd | awk -F: '$3 >= 1000 && $1 != \"nobody\" {print}'"
        )
        users = []
        for line in passwd_out.strip().splitlines():
            parts = line.split(":")
            if len(parts) < 6:
                continue
            username, uid, gid, home = parts[0], parts[2], parts[3], parts[5]
            shadow_line = self._exec(f"getent shadow {username} 2>/dev/null || true")
            shadow_parts = shadow_line.strip().split(":")
            pw_hash = shadow_parts[1] if len(shadow_parts) > 1 else ""
            users.append({"username": username, "uid": uid, "gid": gid, "home": home, "pw_hash": pw_hash})

        aliases = self._exec("cat /etc/aliases 2>/dev/null || true").strip()
        try:
            local_recipients = self._exec("cat /etc/postfix/local_recipients").strip()
        except RuntimeError:
            local_recipients = ""

        return {
            "users": users,
            "aliases": aliases,
            "local_recipients": local_recipients,
        }

    def restore(self, backup: dict, local_mail_dir: Path | None = None) -> None:
        """Recreate users, restore config files, and re-upload local Maildir."""
        assert self._ssh, "Call connect() first"
        sftp = self._ssh.open_sftp()
        try:
            for user in backup.get("users", []):
                username = user["username"]
                pw_hash = user.get("pw_hash", "")
                home = user.get("home", f"/home/{username}")
                self._exec(f"id {username} 2>/dev/null || useradd -m -s /bin/bash {username}")
                if pw_hash and not pw_hash.startswith("!"):
                    sftp.putfo(io.BytesIO(f"{username}:{pw_hash}\n".encode()), "/tmp/.chpasswd_restore")
                    self._exec("chpasswd -e < /tmp/.chpasswd_restore && rm -f /tmp/.chpasswd_restore")
                dirs = _maildir_dirs(home)
                self._exec(
                    f"mkdir -p {dirs} && chown -R {username}:{username} {home}/Maildir"
                )
                self._create_mailboxes(username)

            aliases = backup.get("aliases", "")
            if aliases:
                sftp.putfo(io.BytesIO(aliases.encode()), "/etc/aliases")
                self._exec("newaliases")

            local_recipients = backup.get("local_recipients", "")
            if local_recipients:
                sftp.putfo(io.BytesIO(local_recipients.encode()), "/etc/postfix/local_recipients")
                self._exec("postmap /etc/postfix/local_recipients && systemctl reload postfix")

            if local_mail_dir and local_mail_dir.exists():
                self._upload_maildir(sftp, local_mail_dir, backup.get("users", []))
        finally:
            sftp.close()

    def _upload_maildir(
        self, sftp: paramiko.SFTPClient, local_mail_dir: Path, users: list[dict]
    ) -> None:
        for user in users:
            username = user["username"]
            home = user.get("home", f"/home/{username}")
            remote_base = f"{home}/Maildir"
            for local_name, remote_suffix in _FOLDER_MAP.items():
                local_folder = local_mail_dir / local_name
                if not local_folder.exists():
                    continue
                remote_folder = remote_base if not remote_suffix else f"{remote_base}/{remote_suffix}"
                for sub in ("cur", "new", "tmp"):
                    local_sub = local_folder / sub
                    if not local_sub.exists():
                        continue
                    remote_sub = f"{remote_folder}/{sub}"
                    _sftp_makedirs(sftp, remote_sub)
                    for msg_file in local_sub.iterdir():
                        if msg_file.is_file():
                            sftp.put(str(msg_file), f"{remote_sub}/{msg_file.name}")
            self._exec(f"chown -R {username}:{username} {home}/Maildir")

    def list_users(self) -> list[dict]:
        """Return mail accounts (system users with uid>=1000 that have a Maildir)."""
        assert self._ssh, "Call connect() first"
        out = self._exec(
            "getent passwd | awk -F: '$3 >= 1000 && $1 != \"nobody\" {print $1 \":\" $6}'"
        )
        users = []
        for line in out.strip().splitlines():
            parts = line.split(":", 1)
            if len(parts) != 2:
                continue
            username, home = parts
            has_maildir = self._exec(
                f"test -d {home}/Maildir && echo yes || echo no"
            ).strip() == "yes"
            users.append({"username": username, "home": home, "has_maildir": has_maildir})
        return users

    def reset_password(self, username: str, password: str) -> None:
        """Update a user's password via chpasswd."""
        assert self._ssh, "Call connect() first"
        stdin, stdout, stderr = self._ssh.exec_command("chpasswd")
        stdin.write(f"{username}:{password}\n".encode())
        stdin.channel.shutdown_write()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"chpasswd failed for {username}: {stderr.read().decode()}")

    def fix_maildir(self, username: str) -> None:
        """Create a missing Maildir for an existing user and register standard folders."""
        assert self._ssh, "Call connect() first"
        # Resolve actual home directory from passwd rather than assuming /home/<user>
        home_raw = self._exec(f"getent passwd {username} | cut -d: -f6").strip()
        home = home_raw if home_raw else f"/home/{username}"
        dirs = _maildir_dirs(home)
        self._exec(f"mkdir -p {dirs} && chown -R {username}:{username} {home}/Maildir")
        self._create_mailboxes(username)

    def fix_dovecot_first_valid_uid(self, uid: int) -> None:
        """Lower Dovecot first_valid_uid to allow system users with low UIDs."""
        assert self._ssh, "Call connect() first"
        self._exec(
            "grep -q '^first_valid_uid' /etc/dovecot/conf.d/10-mail.conf && "
            f"sed -i 's/^first_valid_uid.*/first_valid_uid = {uid}/' /etc/dovecot/conf.d/10-mail.conf || "
            f"echo 'first_valid_uid = {uid}' >> /etc/dovecot/conf.d/10-mail.conf"
        )
        self._exec("systemctl restart dovecot")

    def add_user(self, username: str, password: str) -> None:
        assert self._ssh, "Call connect() first"
        self._exec(f"id {username} 2>/dev/null || useradd -m -s /bin/bash {username}")
        # Pass credentials via stdin so the password never appears in a shell command.
        stdin, stdout, stderr = self._ssh.exec_command("chpasswd")
        stdin.write(f"{username}:{password}\n".encode())
        stdin.channel.shutdown_write()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"chpasswd failed: {stderr.read().decode()}")
        dirs = _maildir_dirs(f"/home/{username}")
        self._exec(
            f"mkdir -p {dirs} && chown -R {username}:{username} /home/{username}/Maildir"
        )
        self._create_mailboxes(username)
        self._exec(
            f"grep -qF '^{username} ' /etc/postfix/local_recipients || "
            f"echo '{username} {username}' >> /etc/postfix/local_recipients && "
            f"postmap /etc/postfix/local_recipients && systemctl reload postfix"
        )

    def configure_nginx(self, domain: str) -> None:
        """Install the landing page and configure nginx with the existing Let's Encrypt cert."""
        from ign8mail.thunderbird import generate_autoconfig_xml
        assert self._ssh, "Call connect() first"
        self._exec("apt-get install -y -qq nginx")
        sftp = self._ssh.open_sftp()
        try:
            self._exec("mkdir -p /var/www/ign8mail/.well-known/autoconfig/mail")
            sftp.putfo(io.BytesIO(_LANDING_PAGE.encode()), "/var/www/ign8mail/index.html")
            autoconfig_xml = generate_autoconfig_xml(domain, f"mail.{domain}")
            sftp.putfo(
                io.BytesIO(autoconfig_xml.encode()),
                "/var/www/ign8mail/.well-known/autoconfig/mail/config-v1.1.xml",
            )
            nginx_conf = (
                "server {\n"
                "    listen 80;\n"
                "    listen [::]:80;\n"
                f"    server_name mail.{domain} autoconfig.{domain} {domain};\n"
                "    location /.well-known/acme-challenge/ { root /var/www/html; }\n"
                "    location / { return 301 https://$host$request_uri; }\n"
                "}\n"
                "\n"
                "server {\n"
                "    listen 443 ssl;\n"
                "    listen [::]:443 ssl;\n"
                f"    server_name mail.{domain} autoconfig.{domain};\n"
                "\n"
                f"    ssl_certificate     /etc/letsencrypt/live/mail.{domain}/fullchain.pem;\n"
                f"    ssl_certificate_key /etc/letsencrypt/live/mail.{domain}/privkey.pem;\n"
                "    ssl_protocols TLSv1.2 TLSv1.3;\n"
                "    ssl_prefer_server_ciphers on;\n"
                "\n"
                "    root  /var/www/ign8mail;\n"
                "    index index.html;\n"
                "    location / { try_files $uri $uri/ =404; }\n"
                "}\n"
            )
            sftp.putfo(io.BytesIO(nginx_conf.encode()), "/etc/nginx/sites-available/ign8mail")
        finally:
            sftp.close()
        self._exec(
            "ln -sf /etc/nginx/sites-available/ign8mail /etc/nginx/sites-enabled/ign8mail && "
            "rm -f /etc/nginx/sites-enabled/default && "
            "nginx -t && systemctl enable nginx && systemctl restart nginx"
        )

    def configure_dkim_signing(self, domain: str, dkim_private_key_pem: bytes) -> None:
        """Upload the DKIM key, configure rspamd signing, and wire Postfix milter."""
        assert self._ssh, "Call connect() first"
        self._exec("mkdir -p /etc/rspamd/dkim")
        sftp = self._ssh.open_sftp()
        try:
            key_path = f"/etc/rspamd/dkim/{domain}.mail2.key"
            sftp.putfo(io.BytesIO(dkim_private_key_pem), key_path)
            dkim_conf = (
                "enabled = true;\n"
                "sign_authenticated = true;\n"
                "sign_local = true;\n"
                'use_domain = "header";\n'
                "use_redis = false;\n"
                'path = "/etc/rspamd/dkim/$domain.$selector.key";\n'
                'selector = "mail2";\n'
                "allow_username_mismatch = true;\n"
            )
            sftp.putfo(io.BytesIO(dkim_conf.encode()), "/etc/rspamd/local.d/dkim_signing.conf")
        finally:
            sftp.close()
        self._exec(
            f"chown -R _rspamd:_rspamd /etc/rspamd/dkim && "
            f"chmod 640 /etc/rspamd/dkim/{domain}.mail2.key"
        )
        self._exec("systemctl enable rspamd && systemctl restart rspamd")
        self._exec(
            "postconf -e 'smtpd_milters = inet:localhost:11332' && "
            "postconf -e 'non_smtpd_milters = inet:localhost:11332' && "
            "postconf -e 'milter_default_action = accept' && "
            "postconf -e 'milter_protocol = 6'"
        )
        self._exec("systemctl reload postfix")

    def configure_send_as(self, sender_login_map: dict[str, list[str]]) -> None:
        """Push smtpd_sender_login_maps and require sender↔login match on submission (587).

        Port 25 (MTA-to-MTA) keeps default behavior so external mail still arrives.
        """
        assert self._ssh, "Call connect() first"
        lines = [
            f"{addr} {','.join(sorted(set(users)))}"
            for addr, users in sorted(sender_login_map.items())
            if users
        ]
        content = "\n".join(lines) + "\n"

        sftp = self._ssh.open_sftp()
        try:
            sftp.putfo(io.BytesIO(content.encode()), "/etc/postfix/sender_login_maps")
        finally:
            sftp.close()
        self._exec("postmap /etc/postfix/sender_login_maps")
        self._exec(
            "postconf -e 'smtpd_sender_login_maps = hash:/etc/postfix/sender_login_maps'"
        )

        script = (
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "p = Path('/etc/postfix/master.cf')\n"
            "src = p.read_text().splitlines()\n"
            "wanted = [\n"
            "  '  -o syslog_name=postfix/submission',\n"
            "  '  -o smtpd_tls_security_level=encrypt',\n"
            "  '  -o smtpd_sasl_auth_enable=yes',\n"
            "  '  -o smtpd_tls_auth_only=yes',\n"
            "  '  -o smtpd_sender_login_maps=hash:/etc/postfix/sender_login_maps',\n"
            "  '  -o smtpd_sender_restrictions=reject_sender_login_mismatch,permit_sasl_authenticated,reject',\n"
            "  '  -o smtpd_recipient_restrictions=permit_sasl_authenticated,reject',\n"
            "]\n"
            "out, in_sub = [], False\n"
            "for line in src:\n"
            "    if line.lstrip().startswith('submission inet'):\n"
            "        in_sub = True\n"
            "        out.append(line.lstrip('#'))\n"
            "        out.extend(wanted)\n"
            "        continue\n"
            "    if in_sub:\n"
            "        if line.startswith(('  -o', '  #-o')):\n"
            "            continue\n"
            "        if line and not line.startswith((' ', '\\t')):\n"
            "            in_sub = False\n"
            "    out.append(line)\n"
            "p.write_text('\\n'.join(out) + '\\n')\n"
            "PY"
        )
        self._exec(script)
        self._exec("systemctl reload postfix")

    def enable_autocreate(self) -> None:
        """Deploy the policy daemon and wire it into Postfix recipient checks."""
        assert self._ssh, "Call connect() first"
        sftp = self._ssh.open_sftp()
        try:
            sftp.putfo(io.BytesIO(_POLICY_DAEMON.encode()), "/usr/local/bin/ign8mail-policy")
            sftp.putfo(
                io.BytesIO(_POLICY_SERVICE.encode()),
                "/etc/systemd/system/ign8mail-policy.service",
            )
        finally:
            sftp.close()
        self._exec("chmod +x /usr/local/bin/ign8mail-policy")
        self._exec(
            "systemctl daemon-reload && "
            "systemctl enable ign8mail-policy && "
            "systemctl restart ign8mail-policy"
        )
        self._exec("touch /etc/postfix/silenced_recipients")
        # Write domain so the daemon can filter non-local recipients
        self._exec("postconf -h mydomain > /etc/postfix/ign8mail_domain")
        # policy service must run BEFORE permit_sasl_authenticated so it can
        # create the mailbox before reject_unlisted_recipient checks for it
        self._exec(
            "postconf -e 'smtpd_recipient_restrictions = "
            "permit_mynetworks, check_policy_service inet:127.0.0.1:10023, "
            "permit_sasl_authenticated, reject_unauth_destination, reject_unlisted_recipient'"
        )
        self._exec("systemctl reload postfix")

    def deploy_webmail(
        self,
        domain: str,
        cert_pem: bytes,
        key_pem: bytes,
        passwords: dict[str, str],
        autocreated: dict[str, dict[str, str]],
        users: dict[str, dict] | None = None,
    ) -> None:
        """Build, upload, and start the webmail interface on the server."""
        import subprocess
        import sys
        import tempfile

        assert self._ssh

        project_root = _find_project_root()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", tmpdir],
                capture_output=True,
                text=True,
                cwd=str(project_root),
            )
            if result.returncode != 0:
                raise RuntimeError(f"pip wheel failed:\n{result.stderr}")
            wheels = list(Path(tmpdir).glob("*.whl"))
            if not wheels:
                raise RuntimeError("No wheel produced")
            wheel_path = wheels[0]

            self._exec("apt-get install -y -qq python3-pip")
            self._exec("mkdir -p /opt/ign8mail-webmail /etc/ign8mail /var/lib/ign8mail")

            sftp = self._ssh.open_sftp()
            try:
                sftp.put(str(wheel_path), f"/tmp/{wheel_path.name}")
                import secrets as _secrets
                config: dict[str, object] = {
                    "domain": domain,
                    "imap_host": f"mail.{domain}",
                    "smtp_host": f"mail.{domain}",
                    "passwords": passwords,
                    "autocreated": autocreated,
                    "users": users or {},
                    "secret_key": _secrets.token_hex(32),
                }
                sftp.putfo(
                    io.BytesIO(json.dumps(config, indent=2).encode()),
                    "/etc/ign8mail/webmail.json",
                )
                sftp.putfo(io.BytesIO(cert_pem), "/etc/ign8mail/webmail.crt")
                sftp.putfo(io.BytesIO(key_pem), "/etc/ign8mail/webmail.key")
                sftp.putfo(io.BytesIO(_WEBMAIL_WSGI.encode()), "/opt/ign8mail-webmail/wsgi.py")
                sftp.putfo(
                    io.BytesIO(_WEBMAIL_SERVICE.encode()),
                    "/etc/systemd/system/ign8mail-webmail.service",
                )
            finally:
                sftp.close()

        self._exec("chmod 600 /etc/ign8mail/webmail.json /etc/ign8mail/webmail.key")
        self._exec("apt-get install -y -qq python3-venv")
        self._exec("python3 -m venv /opt/ign8mail-webmail/venv")
        self._exec(
            f"/opt/ign8mail-webmail/venv/bin/pip install --quiet "
            f"--force-reinstall /tmp/{wheel_path.name} gunicorn redis"
        )
        self._exec(
            "systemctl daemon-reload && "
            "systemctl enable ign8mail-webmail && "
            "systemctl restart ign8mail-webmail"
        )
        self._configure_webmail_nginx(domain)

    def _configure_webmail_nginx(self, domain: str) -> None:
        assert self._ssh
        proxy_block = (
            "    location / {\n"
            "        proxy_pass http://127.0.0.1:8080;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-Proto https;\n"
            "    }\n"
        )
        nginx_conf = (
            # Port 80: Cloudflare Flexible SSL mode connects here
            "server {\n"
            "    listen 80;\n"
            "    listen [::]:80;\n"
            f"    server_name webmail.{domain};\n"
            "\n"
            + proxy_block
            + "}\n"
            "\n"
            # Port 443: Cloudflare Full SSL mode connects here (self-signed cert is fine)
            "server {\n"
            "    listen 443 ssl;\n"
            "    listen [::]:443 ssl;\n"
            f"    server_name webmail.{domain};\n"
            "\n"
            "    ssl_certificate     /etc/ign8mail/webmail.crt;\n"
            "    ssl_certificate_key /etc/ign8mail/webmail.key;\n"
            "    ssl_protocols TLSv1.2 TLSv1.3;\n"
            "\n"
            + proxy_block
            + "}\n"
        )
        sftp = self._ssh.open_sftp()
        try:
            sftp.putfo(
                io.BytesIO(nginx_conf.encode()),
                "/etc/nginx/sites-available/ign8mail-webmail",
            )
        finally:
            sftp.close()
        self._exec(
            "ln -sf /etc/nginx/sites-available/ign8mail-webmail "
            "/etc/nginx/sites-enabled/ign8mail-webmail && "
            "nginx -t && systemctl reload nginx"
        )

    def sync_autocreated(self) -> list[dict]:
        """Read and clear the autocreated mailbox log from the server."""
        assert self._ssh, "Call connect() first"
        raw = self._exec("cat /var/log/ign8mail-autocreated.jsonl 2>/dev/null || true")
        entries = []
        for line in raw.strip().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if entries:
            self._exec("truncate -s 0 /var/log/ign8mail-autocreated.jsonl")
        return entries

    def disable_user(self, username: str) -> None:
        """Remove from local_recipients (bounce future mail) but keep the mailbox intact."""
        assert self._ssh, "Call connect() first"
        self._exec(
            f"sed -i '/^{username} /d' /etc/postfix/local_recipients && "
            f"postmap /etc/postfix/local_recipients && systemctl reload postfix"
        )

    def silence_user(self, username: str) -> None:
        """Add to the silenced list and purge the mailbox entirely."""
        assert self._ssh, "Call connect() first"
        self._exec(
            f"grep -qxF '{username}' /etc/postfix/silenced_recipients || "
            f"echo '{username}' >> /etc/postfix/silenced_recipients"
        )
        self.delete_user(username, purge=True)

    def delete_user(self, username: str, *, purge: bool = False) -> None:
        """Remove a mail user from the server.

        Removes from postfix local_recipients and deletes the system user.
        Pass purge=True to also wipe the home directory / Maildir.
        """
        assert self._ssh, "Call connect() first"
        self._exec(
            f"sed -i '/^{username} /d' /etc/postfix/local_recipients && "
            f"postmap /etc/postfix/local_recipients && systemctl reload postfix"
        )
        userdel_flags = "-r" if purge else ""
        self._exec(f"id {username} 2>/dev/null && userdel {userdel_flags} {username} || true")

    def _create_mailboxes(self, username: str) -> None:
        for folder in _STANDARD_MAILBOXES:
            self._exec(f"doveadm mailbox create -u {username} {folder} || true")

    def _configure_postfix(self) -> None:
        cfg = self._cfg
        commands = [
            f"postconf -e 'myhostname = mail.{cfg.domain}'",
            f"postconf -e 'mydomain = {cfg.domain}'",
            "postconf -e 'myorigin = $mydomain'",
            "postconf -e 'inet_interfaces = all'",
            "postconf -e 'inet_protocols = all'",
            "postconf -e 'mydestination = $myhostname, $mydomain, localhost.$mydomain, localhost'",
            "postconf -e 'smtpd_tls_security_level = may'",
            "postconf -e 'smtp_tls_security_level = may'",
            "postconf -e 'home_mailbox = Maildir/'",
            "postconf -e 'smtpd_sasl_auth_enable = yes'",
            "postconf -e 'smtpd_sasl_type = dovecot'",
            "postconf -e 'smtpd_sasl_path = private/auth'",
            f"postconf -e 'message_size_limit = {cfg.max_message_size_mb * 1024 * 1024}'",
            "postconf -e 'local_recipient_maps = hash:/etc/postfix/local_recipients $alias_maps'",
            "postconf -e 'smtpd_recipient_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination, reject_unlisted_recipient'",
            "touch /etc/postfix/local_recipients && postmap /etc/postfix/local_recipients",
            "systemctl enable postfix && systemctl restart postfix",
        ]
        for cmd in commands:
            self._exec(cmd)

    def _configure_dovecot(self) -> None:
        cfg = self._cfg
        self._exec(
            "sed -i 's|^mail_location =.*|mail_location = maildir:~/Maildir|' "
            "/etc/dovecot/conf.d/10-mail.conf"
        )
        self._exec("sed -i 's/^#disable_plaintext_auth/disable_plaintext_auth/' /etc/dovecot/conf.d/10-auth.conf")
        self._exec("sed -i 's/^#ssl = yes/ssl = required/' /etc/dovecot/conf.d/10-ssl.conf")
        # Postfix SASL auth socket
        self._exec(
            "python3 -c \""
            "c=open('/etc/dovecot/conf.d/10-master.conf').read();"
            "c=c.replace("
            "  '  #unix_listener /var/spool/postfix/private/auth {\\\\n  #  mode = 0666\\\\n  #}',"
            "  '  unix_listener /var/spool/postfix/private/auth {\\\\n    mode = 0666\\\\n    user = postfix\\\\n    group = postfix\\\\n  }'"
            ");"
            "open('/etc/dovecot/conf.d/10-master.conf','w').write(c)"
            "\""
        )
        self._exec("systemctl enable dovecot && systemctl restart dovecot")

    def _issue_cert(self) -> None:
        cfg = self._cfg
        self._exec("systemctl stop nginx || true")
        self._exec(
            f"certbot certonly --standalone --non-interactive --agree-tos "
            f"-m {cfg.admin_email} -d mail.{cfg.domain}"
        )
        self._exec("systemctl start nginx || true")
        self._exec(
            f"postconf -e 'smtpd_tls_cert_file = /etc/letsencrypt/live/mail.{cfg.domain}/fullchain.pem'"
        )
        self._exec(
            f"postconf -e 'smtpd_tls_key_file = /etc/letsencrypt/live/mail.{cfg.domain}/privkey.pem'"
        )
        # Dovecot TLS
        self._exec(
            f"sed -i 's|^ssl_cert =.*|ssl_cert = </etc/letsencrypt/live/mail.{cfg.domain}/fullchain.pem|' "
            f"/etc/dovecot/conf.d/10-ssl.conf"
        )
        self._exec(
            f"sed -i 's|^ssl_key =.*|ssl_key = </etc/letsencrypt/live/mail.{cfg.domain}/privkey.pem|' "
            f"/etc/dovecot/conf.d/10-ssl.conf"
        )
        # Enable submission port (587)
        self._exec(
            "sed -i 's/^#submission inet/submission inet/' /etc/postfix/master.cf && "
            "grep -q 'smtpd_recipient_restrictions=permit_sasl_authenticated' /etc/postfix/master.cf || "
            "sed -i '/^submission inet/a\\  -o syslog_name=postfix/submission\\n"
            "  -o smtpd_tls_security_level=encrypt\\n"
            "  -o smtpd_sasl_auth_enable=yes\\n"
            "  -o smtpd_tls_auth_only=yes\\n"
            "  -o smtpd_recipient_restrictions=permit_sasl_authenticated,reject' /etc/postfix/master.cf"
        )
        self._exec("systemctl restart postfix dovecot")

    def tail_log(self, log_path: str = "/var/log/mail.log", *, follow: bool = True) -> None:
        """Stream a remote log file to stdout. Blocks until Ctrl+C (follow=True) or EOF."""
        import select
        import sys

        assert self._ssh
        transport = self._ssh.get_transport()
        assert transport
        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(f"tail {'-f' if follow else ''} {log_path}")
        try:
            while True:
                if channel.exit_status_ready() and not channel.recv_ready():
                    break
                r, _, _ = select.select([channel], [], [], 0.5)
                if r:
                    data = channel.recv(4096)
                    if data:
                        sys.stdout.write(data.decode(errors="replace"))
                        sys.stdout.flush()
        except KeyboardInterrupt:
            pass
        finally:
            channel.close()

    def _drop_known_host(self) -> None:
        host_keys = paramiko.HostKeys()
        if self._known_hosts_path.exists():
            host_keys.load(str(self._known_hosts_path))
        if self._host in host_keys:
            del host_keys[self._host]
            host_keys.save(str(self._known_hosts_path))

    def _save_known_host(self, client: paramiko.SSHClient) -> None:
        transport = client.get_transport()
        if transport is None:
            return
        remote_key = transport.get_remote_server_key()
        host_keys = paramiko.HostKeys()
        if self._known_hosts_path.exists():
            host_keys.load(str(self._known_hosts_path))
        host_keys.add(self._host, remote_key.get_name(), remote_key)
        self._known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        host_keys.save(str(self._known_hosts_path))

    def _exec(self, cmd: str) -> str:
        assert self._ssh
        _, stdout, stderr = self._ssh.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode()
        if exit_code != 0:
            raise RuntimeError(f"Command failed ({exit_code}): {cmd}\n{stderr.read().decode()}")
        return output


def _sftp_makedirs(sftp: paramiko.SFTPClient, remote_path: str) -> None:
    parts = [p for p in remote_path.split("/") if p]
    path = ""
    for part in parts:
        path = f"{path}/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)
