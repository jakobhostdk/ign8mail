"""SSH into the provisioned server and install/configure the mail stack."""

import io
import time
from pathlib import Path

import paramiko

from ign8mail.config import Config

# Packages installed on the server
PACKAGES = "postfix postfix-mysql dovecot-core dovecot-imapd dovecot-pop3d rspamd certbot redis-server"

_FOLDER_MAP = {
    "Inbox": "",
    "Sent": ".Sent",
    "Drafts": ".Drafts",
    "Trash": ".Trash",
    "Junk": ".Junk",
}

# Standard IMAP folders created via doveadm so Dovecot registers them properly.
_STANDARD_MAILBOXES = ("Drafts", "Sent", "Trash", "Junk")


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
        self._exec(
            f"certbot certonly --standalone --non-interactive --agree-tos "
            f"-m {cfg.admin_email} -d mail.{cfg.domain}"
        )
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
