"""CLI entry point: `ign8mail up` provisions the full mail server stack."""

import enum
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ign8mail import vault
from ign8mail.config import Config
from ign8mail.dns.cloudflare import CloudflareDNS
from ign8mail.infra import hetzner
from ign8mail.keys import dkim_private_key_pem, dkim_public_key_base64, save_keypair
from ign8mail.mailserver.setup import MailServerSetup
from ign8mail import thunderbird

app = typer.Typer(name="ign8mail", help="Ignite a production mail server in minutes.")
console = Console()

# Active vault path — set by @app.callback() before any subcommand runs.
_vault: Path = Path(".ign8mail/vault.yml")


@app.callback()
def _main(
    vault_file: Optional[Path] = typer.Option(
        None,
        "--vault",
        envvar="IGN8_VAULT",
        help="Ansible Vault file (default: .ign8mail/vault.yml)",
        show_default=False,
    ),
) -> None:
    """Ignite a production mail server in minutes."""
    global _vault
    if vault_file:
        _vault = vault_file
    if _vault.exists():
        vault.inject_env(_vault)


class _Format(str, enum.Enum):
    pretty = "pretty"
    json = "json"


@app.command()
def up(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Your mail domain (e.g. example.com)"),
    admin_email: str = typer.Option(..., envvar="IGN8_ADMIN_EMAIL", help="Admin / certbot notification email"),
    hetzner_token: str = typer.Option(..., envvar="IGN8_HETZNER_TOKEN", help="Hetzner Cloud API token"),
    cloudflare_token: str = typer.Option(..., envvar="IGN8_CLOUDFLARE_TOKEN", help="Cloudflare API token"),
    cloudflare_zone_id: str = typer.Option(..., envvar="IGN8_CLOUDFLARE_ZONE_ID", help="Cloudflare Zone ID"),
    cloudflare_email: str = typer.Option("", envvar="IGN8_CLOUDFLARE_EMAIL", help="Cloudflare account email (only for Global API Key auth)"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory for keys and state"),
) -> None:
    """Provision a complete mail server: Hetzner VM + Cloudflare DNS + Postfix/Dovecot."""

    cfg = Config(
        domain=domain,
        admin_email=admin_email,  # type: ignore[arg-type]
        hetzner_token=hetzner_token,
        cloudflare_token=cloudflare_token,
        cloudflare_zone_id=cloudflare_zone_id,
        cloudflare_email=cloudflare_email,
    )

    console.print(Panel(f"[bold]ign8mail[/bold] — igniting [cyan]{cfg.domain}[/cyan]"))

    # 1. SSH keypair
    console.print("[1/4] Generating SSH keypair...")
    keys_dir = work_dir / "keys"
    priv_path, pub_path = save_keypair(keys_dir)
    public_key_openssh = pub_path.read_text().strip()

    # 2. Hetzner server
    console.print("[2/4] Provisioning Hetzner server...")
    outputs = hetzner.provision(cfg, public_key_openssh, work_dir / "state.json")
    ipv4: str = outputs["ipv4"]
    ipv6: str = outputs["ipv6"]
    console.print(f"    Server IPv4: [green]{ipv4}[/green]")

    # 3. Cloudflare DNS
    console.print("[3/4] Creating Cloudflare DNS records...")
    dkim_priv, dkim_pub_path = save_keypair(keys_dir, name="dkim")
    dkim_pub_b64 = dkim_public_key_base64(_openssh_pub_to_pem(dkim_pub_path))
    dns = CloudflareDNS(cfg.cloudflare_token, cfg.cloudflare_zone_id, cfg.domain, cfg.cloudflare_email)
    dns.provision(ipv4, ipv6, dkim_pub_b64)

    # 4. Configure mail software
    console.print("[4/4] Installing and configuring Postfix + Dovecot...")
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_path))
    setup.connect()
    setup.run()
    setup.configure_dkim_signing(cfg.domain, dkim_private_key_pem(dkim_priv))
    setup.configure_nginx(cfg.domain)
    setup.enable_autocreate()

    # 5. Restore from backup if one exists
    backup_path = work_dir / "backup.json"
    if backup_path.exists():
        console.print("[5/5] Restoring users and mail from backup...")
        bk = json.loads(backup_path.read_text())
        local_mail_hint = bk.pop("local_mail_hint", None)
        local_mail_dir = Path(local_mail_hint) if local_mail_hint else None
        setup.restore(bk, local_mail_dir)
        backup_path.unlink()
        console.print("    [green]Backup restored and removed.[/green]")

    console.print(Panel(
        f"[bold green]Done![/bold green]\n\n"
        f"SMTP: mail.{cfg.domain}:587 (STARTTLS)\n"
        f"IMAP: mail.{cfg.domain}:993 (SSL)\n\n"
        f"SSH key: {priv_path}\n"
        f"DKIM private key: {dkim_priv}",
        title="Mail server ready",
    ))


@app.command()
def backup(
    hetzner_token: str = typer.Option(..., envvar="IGN8_HETZNER_TOKEN", help="Hetzner Cloud API token"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Snapshot server state (users, config) to backup.json without touching the server."""
    backup_path = work_dir / "backup.json"
    _save_backup(work_dir, hetzner_token, backup_path)


@app.command()
def destroy(
    hetzner_token: str = typer.Option(..., envvar="IGN8_HETZNER_TOKEN", help="Hetzner Cloud API token"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Tear down the Hetzner server, saving a backup first (DNS records are left intact)."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[yellow]No state file found — nothing to destroy.[/yellow]")
        raise typer.Exit()

    backup_path = work_dir / "backup.json"
    _save_backup(work_dir, hetzner_token, backup_path)

    hetzner.destroy(state_path, hetzner_token)
    console.print("[green]Stack destroyed.[/green]")


# ---------------------------------------------------------------------------

@app.command()
def configure_thunderbird(
    username: str = typer.Argument(..., help="Email address or local username (e.g. alice or alice@example.com)"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — required when username has no @"),
    display_name: str = typer.Option("", help="Display name shown in Thunderbird"),
    profile: Path = typer.Option(None, help="Path to Thunderbird profile directory (auto-detected if omitted)"),
) -> None:
    """Add an IMAP/SMTP account to your local Thunderbird profile."""

    if "@" in username:
        email = username
        mail_host = "mail." + email.split("@", 1)[1]
    else:
        if not domain:
            console.print("[red]Error:[/red] provide --domain or set IGN8_DOMAIN when username has no @.")
            raise typer.Exit(1)
        email = f"{username}@{domain}"
        mail_host = f"mail.{domain}"

    name = display_name or email.split("@")[0]

    if profile is None:
        profile = thunderbird.find_profile()
        if profile is None:
            console.print(
                "[red]Error:[/red] Thunderbird profile not found. "
                "Pass --profile /path/to/profile explicitly."
            )
            raise typer.Exit(1)

    console.print(f"Using profile: [cyan]{profile}[/cyan]")
    console.print("[yellow]Close Thunderbird before continuing.[/yellow]")

    thunderbird.configure_account(profile, email, name, mail_host)

    console.print(Panel(
        f"Account: [bold]{email}[/bold]\n"
        f"IMAP:    {mail_host}:993  (SSL/TLS)\n"
        f"SMTP:    {mail_host}:587  (STARTTLS)\n\n"
        "Open Thunderbird — the account will appear automatically.",
        title="Thunderbird configured",
    ))


@app.command()
def read(
    account: str = typer.Argument("", help="Optional: account to focus on launch (e.g. alice or alice@example.com)"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain (defaults to IGN8_DOMAIN)"),
    imap_host: str = typer.Option("", help="IMAP hostname (default: mail.<domain>)"),
    smtp_host: str = typer.Option("", help="SMTP hostname (default: mail.<domain>)"),
) -> None:
    """Open the multi-account TUI mail reader."""
    focus_username: str | None = None
    if account:
        if "@" in account:
            focus_username = account.split("@")[0]
            if not domain:
                domain = account.split("@")[1]
        else:
            focus_username = account

    if not domain:
        console.print("[red]Error:[/red] provide --domain or IGN8_DOMAIN.")
        raise typer.Exit(1)
    if not _vault.exists():
        console.print(f"[red]Error:[/red] vault not found at {_vault}.")
        raise typer.Exit(1)

    imap_host = imap_host or f"mail.{domain}"
    smtp_host = smtp_host or f"mail.{domain}"
    archived_path = _vault.parent / "tui_archived.json"

    from ign8mail.tui.app import MailApp

    MailApp(
        vault_path=_vault,
        archived_path=archived_path,
        domain=domain,
        imap_host=imap_host,
        smtp_host=smtp_host,
        focus_account=focus_username,
    ).run()


@app.command()
def switch_inbox(
    account: str = typer.Argument(..., help="Email address or local username (e.g. alice or alice@example.com)"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — required when account has no @"),
    password: str = typer.Option("", envvar="IGN8_MAIL_PASSWORD", help="IMAP/SMTP password (prompted if account is new)"),
    mail_dir: Path = typer.Option(None, help="Local mail root (default: ~/Mail)"),
    mbsyncrc: Path = typer.Option(None, help="mbsync config path (default: ~/.mbsyncrc)"),
    msmtprc: Path = typer.Option(None, help="msmtp config path (default: ~/.msmtprc)"),
    neomuttrc: Path = typer.Option(None, help="neomutt config path (default: ~/.config/neomutt/neomuttrc)"),
    imap_host: str = typer.Option("", help="IMAP hostname (default: mail.<domain>)"),
    smtp_host: str = typer.Option("", help="SMTP hostname (default: mail.<domain>)"),
    sync: bool = typer.Option(True, "--sync/--no-sync", help="Run mbsync after switching"),
) -> None:
    """Switch mbsync and neomutt to point to a different mailbox."""

    if "@" in account:
        email = account
        username = account.split("@")[0]
        if not domain:
            domain = account.split("@")[1]
    else:
        if not domain:
            console.print("[red]Error:[/red] provide --domain or IGN8_DOMAIN when account has no @.")
            raise typer.Exit(1)
        username = account
        email = f"{username}@{domain}"

    home = Path.home()
    mbsyncrc = mbsyncrc or home / ".mbsyncrc"
    msmtprc = msmtprc or home / ".msmtprc"
    neomuttrc = neomuttrc or home / ".config" / "neomutt" / "neomuttrc"
    mail_dir = mail_dir or home / "Mail"
    imap_host = imap_host or f"mail.{domain}"
    smtp_host = smtp_host or f"mail.{domain}"
    user_maildir = mail_dir / username

    mbsync_profile = mbsyncrc.parent / f".mbsync.{username}"
    needs_password = not mbsync_profile.exists() or not _msmtp_has_account(msmtprc, username)
    if needs_password and not password:
        if _vault.exists():
            password = vault.get_mailbox_password(username, _vault)
        else:
            password = typer.prompt(f"Password for {email}", hide_input=True)

    user_maildir.mkdir(parents=True, exist_ok=True)

    if not mbsync_profile.exists():
        console.print(f"Writing mbsync profile [cyan]{mbsync_profile}[/cyan]...")
        _mbsync_write_profile(mbsync_profile, username, password, imap_host, user_maildir)
    else:
        console.print(f"mbsync: profile for [cyan]{username}[/cyan] already exists")
    _mbsync_switch_symlink(mbsyncrc, username)
    console.print(f"mbsync: [dim]{mbsyncrc}[/dim] → [cyan].mbsync.{username}[/cyan]")

    if not _msmtp_has_account(msmtprc, username):
        console.print(f"Adding [cyan]{username}[/cyan] to {msmtprc}...")
        _msmtp_add_account(msmtprc, username, email, password, smtp_host)
    _msmtp_set_default(msmtprc, username)
    console.print(f"msmtp: default → [cyan]{username}[/cyan]")

    console.print(f"neomutt: switching to [cyan]{email}[/cyan]...")
    _neomutt_switch(neomuttrc, username, email, user_maildir)

    if sync:
        if shutil.which("mbsync"):
            console.print(f"Syncing [dim]mbsync {username}[/dim]...")
            subprocess.run(["mbsync", username], check=False)
        else:
            console.print("[yellow]mbsync not in PATH — skipping sync[/yellow]")

    console.print(Panel(
        f"Account:  [bold]{email}[/bold]\n"
        f"Maildir:  {user_maildir}\n"
        f"IMAP:     {imap_host}:993\n"
        f"SMTP:     {smtp_host}:587",
        title="[bold green]Inbox switched[/bold green]",
    ))


@app.command()
def adduser(
    username: str = typer.Argument(..., help="Linux username (local part of the email address)"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display only"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Create a mail user on the running server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    password = vault.get_mailbox_password(username, _vault)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        setup.add_user(username, password)
    finally:
        setup.disconnect()

    email = f"{username}@{domain}" if domain else username
    console.print(f"[green]User [bold]{email}[/bold] created.[/green]")


@app.command()
def create_mailbox(
    mailboxname: str = typer.Argument(..., help="Mailbox name / local part of the email address (e.g. alice)"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display only"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Create a mailbox on the running server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    password = vault.get_mailbox_password(mailboxname, _vault)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        setup.add_user(mailboxname, password)
    finally:
        setup.disconnect()

    email = f"{mailboxname}@{domain}" if domain else mailboxname
    console.print(f"[green]Mailbox [bold]{email}[/bold] created.[/green]")


@app.command()
def delete_mailbox(
    mailboxname: str = typer.Argument(..., help="Mailbox name / local part of the email address (e.g. alice)"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display only"),
    purge: bool = typer.Option(False, "--purge", help="Also delete the home directory and all mail data"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Delete a mailbox from the running server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    email = f"{mailboxname}@{domain}" if domain else mailboxname
    if purge:
        typer.confirm(
            f"Permanently delete mailbox {email} including all mail data?",
            abort=True,
        )

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        setup.delete_user(mailboxname, purge=purge)
    finally:
        setup.disconnect()

    console.print(f"[green]Mailbox [bold]{email}[/bold] deleted.[/green]")
    if purge:
        console.print("[dim]Home directory and mail data removed.[/dim]")


@app.command()
def configure_nginx(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Mail domain"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Deploy the landing page and configure nginx on the running server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        setup.configure_nginx(domain)
    finally:
        setup.disconnect()

    console.print(f"[green]nginx landing page live at [bold]https://mail.{domain}[/bold][/green]")


@app.command()
def configure_dkim(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Mail domain"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Upload the DKIM key and configure rspamd signing on the running server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    dkim_priv = work_dir / "keys" / "dkim"
    if not dkim_priv.exists():
        console.print(f"[red]Error:[/red] DKIM private key not found at {dkim_priv}.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        setup.configure_dkim_signing(domain, dkim_private_key_pem(dkim_priv))
    finally:
        setup.disconnect()

    console.print(f"[green]DKIM signing configured for [bold]{domain}[/bold] (selector: mail).[/green]")


@app.command()
def enable_autocreate(
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Deploy the autocreate policy daemon on the running server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        setup.enable_autocreate()
    finally:
        setup.disconnect()

    console.print("[green]Autocreate policy daemon deployed and Postfix updated.[/green]")


@app.command()
def seed(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Mail domain"),
    mailboxes_file: Path = typer.Option(
        Path("mailboxes.yml"), help="YAML file listing desired mailboxes"
    ),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
    sender: str = typer.Option(
        "", help="Local-part of the authenticated sender (default: first account in vault)"
    ),
    sender_password: str = typer.Option(
        "", help="Override sender's SMTP password (default: from vault)"
    ),
    smtp_host: str = typer.Option("", help="SMTP submission host (default: mail.<domain>)"),
    smtp_port: int = typer.Option(587, help="SMTP submission port"),
    subject: str = typer.Option("ign8mail seed", help="Subject of the seed message"),
    wait: float = typer.Option(
        3.0, help="Seconds to wait for the policy daemon to write its log"
    ),
) -> None:
    """Send a seed email to each address in mailboxes.yml so the server's autocreate
    daemon provisions the mailbox, then sync the new passwords into the vault.

    Mail must come from outside the server (port 587, SASL auth) — Postfix's
    `permit_mynetworks` would otherwise short-circuit the policy check.
    """
    import smtplib
    import ssl
    import time
    from email.message import EmailMessage

    import yaml

    if not mailboxes_file.exists():
        console.print(f"[red]Error:[/red] mailbox list not found at {mailboxes_file}.")
        raise typer.Exit(1)
    try:
        data = yaml.safe_load(mailboxes_file.read_text()) or {}
    except yaml.YAMLError as e:
        console.print(f"[red]Error:[/red] could not parse {mailboxes_file}: {e}")
        raise typer.Exit(1)
    desired: list[str] = [str(x).strip() for x in (data.get("mailboxes") or []) if str(x).strip()]
    if not desired:
        console.print(f"[yellow]No mailboxes listed in {mailboxes_file}.[/yellow]")
        return

    if not _vault.exists():
        console.print(f"[red]Error:[/red] vault not found at {_vault}.")
        raise typer.Exit(1)
    vault_data = vault.load(_vault)
    passwords: dict[str, str] = vault_data.get("mailbox_passwords") or {}

    if not sender:
        if not passwords:
            console.print(
                "[red]Error:[/red] no existing accounts in the vault to authenticate as. "
                "Create one first with [bold]ign8mail create-mailbox <name>[/bold]."
            )
            raise typer.Exit(1)
        sender = sorted(passwords.keys())[0]
    if not sender_password:
        sender_password = passwords.get(sender, "")
    if not sender_password:
        console.print(f"[red]Error:[/red] no password for sender {sender!r} in vault.")
        raise typer.Exit(1)

    smtp_host = smtp_host or f"mail.{domain}"
    sender_addr = f"{sender}@{domain}"

    to_seed = [n for n in desired if n not in passwords]
    already_known = [n for n in desired if n in passwords]
    if not to_seed:
        console.print("[green]All requested mailboxes are already in the vault.[/green]")
        if already_known:
            console.print(f"[dim]Known: {', '.join(already_known)}[/dim]")
        return

    console.print(
        f"Sending seed mail from [cyan]{sender_addr}[/cyan] via "
        f"{smtp_host}:{smtp_port} to [bold]{len(to_seed)}[/bold] recipient(s)…"
    )

    ctx = ssl.create_default_context()
    sent: list[str] = []
    failed: list[tuple[str, str]] = []
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(sender, sender_password)
            for name in to_seed:
                recipient = f"{name}@{domain}"
                msg = EmailMessage()
                msg["From"] = sender_addr
                msg["To"] = recipient
                msg["Subject"] = subject
                msg.set_content(
                    f"This message was sent by `ign8mail seed` to bootstrap {recipient}."
                )
                try:
                    s.send_message(msg)
                    sent.append(name)
                except smtplib.SMTPRecipientsRefused as e:
                    # Most common: the policy daemon DID create the user but its
                    # postmap write hadn't propagated before Postfix re-checked
                    # `reject_unlisted_recipient`. The user usually exists anyway —
                    # the sync below will confirm.
                    refused = next(iter(e.recipients.values()), (0, b""))
                    code, resp = refused[0], refused[1]
                    detail = resp.decode("latin-1", errors="replace") if isinstance(resp, bytes) else str(resp)
                    failed.append((name, f"SMTP {code}: {detail.strip()}"))
                except smtplib.SMTPException as e:
                    failed.append((name, f"{type(e).__name__}: {e}"))
    except Exception as e:
        console.print(f"[red]SMTP error:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(1) from e

    if wait > 0 and sent:
        console.print(f"[dim]Waiting {wait:.1f}s for policy daemon to write its log…[/dim]")
        time.sleep(wait)

    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print(f"[red]Error:[/red] no state file at {state_path}.")
        raise typer.Exit(1)
    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"
    if not ipv4 or not priv_key.exists():
        console.print("[red]Error:[/red] missing server IP or SSH key.")
        raise typer.Exit(1)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        new_entries = setup.sync_autocreated()
    finally:
        setup.disconnect()

    vault_data.setdefault("mailbox_passwords", {})
    vault_data.setdefault("autocreated_mailboxes", {})
    synced: list[str] = []
    for e in new_entries:
        name = e["username"]
        vault_data["mailbox_passwords"][name] = e["password"]
        vault_data["autocreated_mailboxes"][name] = {
            "password": e["password"],
            "status": "pending",
            "ts": e.get("ts", 0),
        }
        synced.append(name)
    if synced:
        vault.save(vault_data, _vault)

    fail_map = dict(failed)
    synced_set = set(synced)
    table = Table(title=f"Seed results — {domain}", show_lines=False)
    table.add_column("Mailbox", style="cyan")
    table.add_column("Result")
    table.add_column("Detail")
    truly_failed = 0
    for name in desired:
        if name in already_known:
            table.add_row(f"{name}@{domain}", "[dim]skip[/dim]", "already in vault")
        elif name in synced_set:
            # The autocreate log proves the mailbox was created on the server,
            # even if SMTP returned 550 (recipient-table re-check race).
            note = "" if name in sent else "created via policy daemon"
            table.add_row(f"{name}@{domain}", "[green]✓ created[/green]", note)
        else:
            truly_failed += 1
            reason = fail_map.get(name, "no log entry — re-run `ign8mail seed`")
            table.add_row(f"{name}@{domain}", "[red]✗[/red]", reason)
    console.print(table)
    console.print(
        f"[bold green]{len(synced)} created[/bold green]  ·  "
        f"[dim]{len(already_known)} already known[/dim]  ·  "
        f"[red]{truly_failed} failed[/red]"
    )


# ---------------------------------------------------------------------------
# autocreated subapp
# ---------------------------------------------------------------------------

_autocreated_app = typer.Typer(name="autocreated", help="Manage auto-created mailboxes.")
app.add_typer(_autocreated_app)


def _load_autocreated(vault_path: Path) -> dict[str, dict]:
    if not vault_path.exists():
        return {}
    data = vault.load(vault_path)
    return data.get("autocreated_mailboxes") or {}


def _save_autocreated(entries: dict[str, dict], vault_path: Path) -> None:
    data = vault.load(vault_path) if vault_path.exists() else {}
    data["autocreated_mailboxes"] = entries
    vault.save(data, vault_path)


def _autocreated_setup(work_dir: Path) -> tuple[str, Path]:
    """Return (ipv4, priv_key) or raise typer.Exit."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)
    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"
    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)
    return ipv4, priv_key


def _make_setup(ipv4: str, priv_key: Path) -> MailServerSetup:
    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    return MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))


@_autocreated_app.command(name="list")
def autocreated_list(
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display"),
) -> None:
    """List auto-created mailboxes and their status."""
    entries = _load_autocreated(_vault)
    if not entries:
        console.print("[yellow]No auto-created mailboxes recorded.[/yellow]")
        return

    _STATUS_COLOR = {
        "pending": "yellow",
        "enabled": "green",
        "disabled": "dim",
        "silenced": "red",
    }
    table = Table(title="Auto-created mailboxes", show_lines=False)
    table.add_column("Username", style="cyan")
    table.add_column("Email" if domain else "Account")
    table.add_column("Status", justify="center")

    for name, info in sorted(entries.items()):
        status = info.get("status", "pending")
        color = _STATUS_COLOR.get(status, "white")
        email = f"{name}@{domain}" if domain else name
        table.add_row(name, email, f"[{color}]{status}[/{color}]")

    console.print(table)


@_autocreated_app.command()
def sync(
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Pull new auto-created mailboxes from the server into the vault."""
    ipv4, priv_key = _autocreated_setup(work_dir)
    setup = _make_setup(ipv4, priv_key)
    setup.connect(retries=3, delay=5)
    try:
        new_entries = setup.sync_autocreated()
    finally:
        setup.disconnect()

    if not new_entries:
        console.print("[yellow]No new auto-created mailboxes on server.[/yellow]")
        return

    entries = _load_autocreated(_vault)
    vault_data = vault.load(_vault) if _vault.exists() else {}
    vault_data.setdefault("mailbox_passwords", {})
    for e in new_entries:
        name = e["username"]
        if name not in entries:
            entries[name] = {"password": e["password"], "status": "pending", "ts": e.get("ts", 0)}
        vault_data["mailbox_passwords"].setdefault(name, e["password"])
    vault_data["autocreated_mailboxes"] = entries
    vault.save(vault_data, _vault)

    table = Table(title="Synced auto-created mailboxes", show_lines=False)
    table.add_column("Username", style="cyan")
    table.add_column("Email" if domain else "Account")
    table.add_column("Status")

    for e in new_entries:
        name = e["username"]
        email = f"{name}@{domain}" if domain else name
        table.add_row(name, email, "[yellow]pending[/yellow]")

    console.print(table)
    console.print(f"[dim]Run [bold]ign8mail autocreated list[/bold] to review.[/dim]")


@_autocreated_app.command()
def enable(
    name: str = typer.Argument(..., help="Username of the auto-created mailbox"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display"),
) -> None:
    """Keep an auto-created mailbox (mark it as enabled)."""
    entries = _load_autocreated(_vault)
    if name not in entries:
        console.print(f"[red]Error:[/red] '{name}' not found in auto-created mailboxes.")
        raise typer.Exit(1)
    entries[name]["status"] = "enabled"
    _save_autocreated(entries, _vault)
    email = f"{name}@{domain}" if domain else name
    console.print(f"[green]Mailbox [bold]{email}[/bold] enabled.[/green]")


@_autocreated_app.command()
def disable(
    name: str = typer.Argument(..., help="Username of the auto-created mailbox"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Stop accepting new mail to an auto-created mailbox (existing mail kept)."""
    entries = _load_autocreated(_vault)
    if name not in entries:
        console.print(f"[red]Error:[/red] '{name}' not found in auto-created mailboxes.")
        raise typer.Exit(1)

    ipv4, priv_key = _autocreated_setup(work_dir)
    setup = _make_setup(ipv4, priv_key)
    setup.connect(retries=3, delay=5)
    try:
        setup.disable_user(name)
    finally:
        setup.disconnect()

    entries[name]["status"] = "disabled"
    _save_autocreated(entries, _vault)
    email = f"{name}@{domain}" if domain else name
    console.print(f"[yellow]Mailbox [bold]{email}[/bold] disabled — future mail will bounce.[/yellow]")


@_autocreated_app.command()
def silence(
    name: str = typer.Argument(..., help="Username to silence"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Delete an auto-created mailbox and permanently block future creation."""
    entries = _load_autocreated(_vault)

    ipv4, priv_key = _autocreated_setup(work_dir)
    setup = _make_setup(ipv4, priv_key)
    setup.connect(retries=3, delay=5)
    try:
        setup.silence_user(name)
    finally:
        setup.disconnect()

    entries.pop(name, None)
    entries[name] = {"status": "silenced"}
    _save_autocreated(entries, _vault)
    vault_data = vault.load(_vault) if _vault.exists() else {}
    vault_data.get("mailbox_passwords", {}).pop(name, None)
    vault.save(vault_data, _vault)

    email = f"{name}@{domain}" if domain else name
    console.print(f"[red]Mailbox [bold]{email}[/bold] silenced and purged.[/red]")


@app.command()
def dmarc_stats(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Mail domain"),
    imap_host: str = typer.Option("", help="IMAP hostname (default: mail.<domain>)"),
    mailbox: str = typer.Option("dmarc", help="Local part of the DMARC reports address"),
    delete: bool = typer.Option(False, "--delete", help="Delete parsed report emails after reading"),
) -> None:
    """Fetch and display DMARC aggregate report statistics."""
    import gzip
    import imaplib
    import io
    import ssl
    import zipfile
    import xml.etree.ElementTree as ET
    import email as emaillib
    from datetime import datetime, timezone

    host = imap_host or f"mail.{domain}"
    password = vault.get_mailbox_password(mailbox, _vault)

    ctx = ssl.create_default_context()
    try:
        imap = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx)
    except Exception as exc:
        console.print(f"[red]IMAP connection failed:[/red] {exc}")
        raise typer.Exit(1)

    imap.login(mailbox, password)
    imap.select("INBOX")
    _, data = imap.search(None, "ALL")
    ids = data[0].split()

    if not ids:
        console.print(f"[yellow]No DMARC reports in {mailbox}@{domain}.[/yellow]")
        imap.logout()
        return

    records: list[dict] = []

    for msg_id in ids:
        _, msg_data = imap.fetch(msg_id, "(RFC822)")
        msg = emaillib.message_from_bytes(msg_data[0][1])

        for part in msg.walk():
            ct = part.get_content_type()
            fn = part.get_filename() or ""
            payload = part.get_payload(decode=True)
            if not payload:
                continue

            xml_bytes: bytes | None = None
            if fn.endswith(".zip") or ct == "application/zip":
                with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                    for name in zf.namelist():
                        if name.endswith(".xml"):
                            xml_bytes = zf.read(name)
                            break
            elif fn.endswith(".gz") or ct in ("application/gzip", "application/x-gzip"):
                xml_bytes = gzip.decompress(payload)
            elif fn.endswith(".xml") or ct == "application/xml":
                xml_bytes = payload

            if not xml_bytes:
                continue

            try:
                root = ET.fromstring(xml_bytes)
            except ET.ParseError:
                continue

            meta = root.find("report_metadata")
            org = meta.findtext("org_name", "") if meta is not None else ""
            begin = int(meta.findtext("date_range/begin", "0") or 0) if meta is not None else 0
            end = int(meta.findtext("date_range/end", "0") or 0) if meta is not None else 0

            for rec in root.findall("record"):
                row = rec.find("row")
                if row is None:
                    continue
                source_ip = row.findtext("source_ip", "")
                count = int(row.findtext("count", "1") or 1)
                pe = row.find("policy_evaluated")
                disposition = pe.findtext("disposition", "") if pe is not None else ""
                dkim_res = pe.findtext("dkim", "") if pe is not None else ""
                spf_res = pe.findtext("spf", "") if pe is not None else ""
                records.append({
                    "org": org,
                    "source_ip": source_ip,
                    "count": count,
                    "disposition": disposition,
                    "dkim": dkim_res,
                    "spf": spf_res,
                    "begin": begin,
                    "end": end,
                })

        if delete:
            imap.store(msg_id, "+FLAGS", "\\Deleted")

    if delete:
        imap.expunge()
    imap.logout()

    if not records:
        console.print("[yellow]No parseable DMARC records found.[/yellow]")
        return

    def _color_result(val: str) -> str:
        return f"[green]{val}[/green]" if val == "pass" else f"[red]{val}[/red]" if val == "fail" else val

    def _color_disp(val: str) -> str:
        return f"[green]{val}[/green]" if val == "none" else f"[yellow]{val}[/yellow]"

    table = Table(title=f"DMARC aggregate reports — {domain}", show_lines=False)
    table.add_column("Org", style="cyan")
    table.add_column("Source IP")
    table.add_column("Count", justify="right")
    table.add_column("DKIM", justify="center")
    table.add_column("SPF", justify="center")
    table.add_column("Disposition", justify="center")
    table.add_column("Period")

    for r in sorted(records, key=lambda x: (-x["count"], x["org"])):
        begin_str = datetime.fromtimestamp(r["begin"], tz=timezone.utc).strftime("%Y-%m-%d") if r["begin"] else ""
        end_str = datetime.fromtimestamp(r["end"], tz=timezone.utc).strftime("%Y-%m-%d") if r["end"] else ""
        period = f"{begin_str} → {end_str}" if begin_str else ""
        table.add_row(
            r["org"],
            r["source_ip"],
            str(r["count"]),
            _color_result(r["dkim"]),
            _color_result(r["spf"]),
            _color_disp(r["disposition"]),
            period,
        )

    console.print(table)
    total = sum(r["count"] for r in records)
    passed = sum(r["count"] for r in records if r["dkim"] == "pass" and r["spf"] == "pass")
    console.print(f"\n[dim]Total messages: {total} — fully passing: {passed} ({100*passed//total}%)[/dim]")


@app.command(name="list")
def list_inboxes(
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — used for display"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """List known mail inboxes on the running server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        users = setup.list_users()
    finally:
        setup.disconnect()

    if not users:
        console.print("[yellow]No mail accounts found.[/yellow]")
        return

    table = Table(title="Mail inboxes", show_lines=False)
    table.add_column("Username", style="cyan")
    table.add_column("Email" if domain else "Account")
    table.add_column("Maildir", justify="center")

    for u in users:
        email = f"{u['username']}@{domain}" if domain else u["username"]
        maildir_str = "[green]yes[/green]" if u["has_maildir"] else "[yellow]no[/yellow]"
        table.add_row(u["username"], email, maildir_str)

    console.print(table)


@app.command(name="passwd-check")
def passwd_check(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Mail domain"),
    imap_host: str = typer.Option("", help="IMAP hostname (default: mail.<domain>)"),
    workers: int = typer.Option(8, help="Parallel login probes"),
    timeout: float = typer.Option(10.0, help="Per-probe timeout in seconds"),
) -> None:
    """Probe IMAP login for every account in the vault — non-destructive.

    Reports which vault passwords succeed/fail against the live server, so you can
    spot drift before running `ign8mail passwd-recycle`.
    """
    import imaplib
    import ssl
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not _vault.exists():
        console.print(f"[red]Error:[/red] vault not found at {_vault}.")
        raise typer.Exit(1)

    vault_data = vault.load(_vault)
    passwords: dict[str, str] = vault_data.get("mailbox_passwords") or {}
    if not passwords:
        console.print("[yellow]No mailbox passwords in vault.[/yellow]")
        return

    host = imap_host or f"mail.{domain}"
    ctx = ssl.create_default_context()

    def probe(username: str, password: str) -> tuple[str, str, str]:
        if not password:
            return username, "SKIP", "no password stored"
        try:
            imap = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx, timeout=timeout)
        except Exception as e:
            return username, "FAIL", f"connect: {type(e).__name__}: {e}"
        try:
            imap.login(username, password)
        except imaplib.IMAP4.error as e:
            detail = e.args[0] if e.args else str(e)
            if isinstance(detail, bytes):
                detail = detail.decode("latin-1", errors="replace")
            return username, "FAIL", str(detail).strip()
        except Exception as e:
            return username, "FAIL", f"{type(e).__name__}: {e}"
        try:
            imap.logout()
        except Exception:
            pass
        return username, "OK", ""

    results: list[tuple[str, str, str]] = []
    with console.status(f"Probing {len(passwords)} accounts on {host}…"):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe, u, p) for u, p in sorted(passwords.items())]
            for fut in as_completed(futures):
                results.append(fut.result())

    # FAILs first, then SKIPs, then OKs — alpha within each group
    rank = {"FAIL": 0, "SKIP": 1, "OK": 2}
    results.sort(key=lambda r: (rank.get(r[1], 3), r[0]))

    table = Table(title=f"Vault credential check — {host}", show_lines=False)
    table.add_column("Username", style="cyan")
    table.add_column("Email")
    table.add_column("Status")
    table.add_column("Detail")
    counts = {"OK": 0, "FAIL": 0, "SKIP": 0}
    for username, status, detail in results:
        counts[status] = counts.get(status, 0) + 1
        if status == "OK":
            cell = "[green]OK[/green]"
        elif status == "SKIP":
            cell = "[dim]SKIP[/dim]"
        else:
            cell = "[red]FAIL[/red]"
        table.add_row(username, f"{username}@{domain}", cell, detail)
    console.print(table)
    console.print(
        f"[bold green]{counts['OK']} OK[/bold green]  ·  "
        f"[bold red]{counts['FAIL']} FAIL[/bold red]  ·  "
        f"[dim]{counts['SKIP']} skipped[/dim]"
    )
    if counts["FAIL"]:
        console.print(
            "[dim]Fix per account on the server, or run "
            "[bold]ign8mail passwd-recycle[/bold] to reset everything.[/dim]"
        )
        raise typer.Exit(1)


@app.command()
def passwd_recycle(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Mail domain"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
    mail_dir: Path = typer.Option(None, help="Local mail root (default: ~/Mail)"),
    mbsyncrc: Path = typer.Option(None, help="mbsync symlink path (default: ~/.mbsyncrc)"),
    msmtprc: Path = typer.Option(None, help="msmtp config path (default: ~/.msmtprc)"),
    imap_host: str = typer.Option("", help="IMAP hostname (default: mail.<domain>)"),
) -> None:
    """Reset all mail account passwords and write per-mailbox ~/.mbsync.<user> profiles."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    home = Path.home()
    mbsyncrc = mbsyncrc or home / ".mbsyncrc"
    msmtprc = msmtprc or home / ".msmtprc"
    mail_dir = mail_dir or home / "Mail"
    imap_host = imap_host or f"mail.{domain}"

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    try:
        users = setup.list_users()
        if not users:
            console.print("[yellow]No mail accounts found.[/yellow]")
            return
        new_passwords: dict[str, str] = {u["username"]: secrets.token_hex(12) for u in users}
        for username, password in new_passwords.items():
            setup.reset_password(username, password)
            console.print(f"    Reset [cyan]{username}[/cyan]")
    finally:
        setup.disconnect()

    if _vault.exists():
        vault_data = vault.load(_vault)
        vault_data.setdefault("mailbox_passwords", {})
        vault_data["mailbox_passwords"].update(new_passwords)
        vault.save(vault_data, _vault)
        console.print(f"[dim]New passwords saved to vault {_vault}[/dim]")

    table = Table(title="New credentials", show_lines=False)
    table.add_column("Username", style="cyan")
    table.add_column("Email")
    table.add_column("Password", style="yellow")
    table.add_column("Profile")

    for username, password in new_passwords.items():
        email = f"{username}@{domain}"
        user_maildir = mail_dir / username
        user_maildir.mkdir(parents=True, exist_ok=True)
        profile = mbsyncrc.parent / f".mbsync.{username}"
        _mbsync_write_profile(profile, username, password, imap_host, user_maildir)
        if _msmtp_has_account(msmtprc, username):
            _msmtp_update_password(msmtprc, username, password)
        table.add_row(username, email, password, str(profile))

    console.print(table)
    console.print(f"\n[dim]Run [bold]ign8mail switch-inbox <user>[/bold] to activate a mailbox.[/dim]")


@app.command()
def tail_postfix_log(
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-f", help="Stream new lines as they arrive"),
    log: str = typer.Option("/var/log/mail.log", help="Log file path on the server"),
) -> None:
    """Stream the Postfix log from the running server (Ctrl+C to stop)."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    cfg = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
    setup.connect(retries=3, delay=5)
    console.print(f"[dim]Streaming {log} — Ctrl+C to stop[/dim]")
    try:
        setup.tail_log(log, follow=follow)
    finally:
        setup.disconnect()


@app.command()
def status(
    hetzner_token: str = typer.Option("", envvar="IGN8_HETZNER_TOKEN", help="Hetzner Cloud API token (enables live server check)"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
    check_services: bool = typer.Option(False, "--check-services", help="SSH into the server and check mail service health"),
    check_mail: bool = typer.Option(False, "--check-mail", help="Send a test email and verify delivery via IMAP"),
    mail_user: str = typer.Option("", "--mail-user", help="Username (local part) for --check-mail"),
    mail_password: str = typer.Option("", "--mail-password", envvar="IGN8_MAIL_PASSWORD", help="Password for --check-mail (prompted if omitted)"),
    domain: str = typer.Option("", envvar="IGN8_DOMAIN", help="Mail domain — required for --check-mail"),
    format: _Format = typer.Option(_Format.pretty, help="Output format: pretty or json"),
) -> None:
    """Show the current state of the provisioned mail server."""
    state_path = work_dir / "state.json"
    priv_key = work_dir / "keys" / "ign8mail"

    info: dict = {
        "provisioned": state_path.exists(),
        "keys_present": priv_key.exists(),
        "ipv4": None,
        "ipv6": None,
        "server_id": None,
        "server_status": None,
        "services": None,
        "mail_test": None,
    }

    if state_path.exists():
        state = json.loads(state_path.read_text())
        info["ipv4"] = state.get("ipv4")
        info["ipv6"] = state.get("ipv6")
        info["server_id"] = state.get("server_id")

    if hetzner_token and info["server_id"]:
        try:
            from hcloud import Client
            client = Client(token=hetzner_token)
            server = client.servers.get_by_id(info["server_id"])
            info["server_status"] = server.status if server else "not_found"
        except Exception as exc:
            info["server_status"] = f"error: {exc}"

    if check_services and info["ipv4"] and priv_key.exists():
        cfg = Config(
            domain="placeholder",
            admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
            hetzner_token="placeholder",
            cloudflare_token="placeholder",
            cloudflare_zone_id="placeholder",
        )
        setup = MailServerSetup(cfg, host=info["ipv4"], private_key_path=str(priv_key))
        try:
            setup.connect(retries=2, delay=3)
            services: dict[str, str] = {}
            for svc in ("postfix", "dovecot", "rspamd"):
                try:
                    out = setup._exec(f"systemctl is-active {svc}").strip()
                    services[svc] = out
                except Exception:
                    services[svc] = "inactive"
            setup.disconnect()
            info["services"] = services
        except Exception as exc:
            info["services"] = {"error": str(exc)}

    if check_mail:
        if not domain:
            console.print("[red]Error:[/red] --domain / IGN8_DOMAIN is required for --check-mail.")
            raise typer.Exit(1)
        if not mail_user:
            console.print("[red]Error:[/red] --mail-user is required for --check-mail.")
            raise typer.Exit(1)
        if not mail_password:
            if _vault.exists():
                mail_password = vault.get_mailbox_password(mail_user, _vault)
            else:
                mail_password = typer.prompt("Mail password", hide_input=True)
        info["mail_test"] = _check_mail(f"mail.{domain}", mail_user, mail_password, domain)

    if format == _Format.json:
        console.print_json(json.dumps(info))
    else:
        _print_status_pretty(info)


@app.command()
def vault_init(
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory for the vault file"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing vault"),
) -> None:
    """Create .ign8mail/vault.yml from current IGN8_* environment variables.

    Set IGN8MAIL_TOKEN before running — that becomes the vault encryption password.
    """
    vault_path = work_dir / "vault.yml"
    if vault_path.exists() and not force:
        console.print(
            f"[yellow]Vault already exists at {vault_path}.[/yellow]  "
            "Use [bold]--force[/bold] to overwrite."
        )
        raise typer.Exit()

    _ENV_KEYS = [
        "IGN8_HETZNER_TOKEN",
        "IGN8_CLOUDFLARE_TOKEN",
        "IGN8_CLOUDFLARE_ZONE_ID",
        "IGN8_CLOUDFLARE_EMAIL",
    ]
    data: dict = {k: os.environ.get(k, "") for k in _ENV_KEYS}
    data.setdefault("mailbox_passwords", {})

    work_dir.mkdir(parents=True, exist_ok=True)
    vault.save(data, vault_path)
    vault_path.chmod(0o600)

    filled = [k for k in _ENV_KEYS if data[k]]
    empty = [k for k in _ENV_KEYS if not data[k]]

    console.print(Panel(
        "\n".join(
            ([f"[green]stored:[/green]  " + ", ".join(filled)] if filled else [])
            + ([f"[yellow]missing:[/yellow] " + ", ".join(empty)] if empty else [])
            + ["", f"Edit with:  [bold]ansible-vault edit {vault_path}[/bold]"]
        ),
        title=f"[bold green]Vault created:[/bold green] {vault_path}",
    ))


@app.command()
def webmail(
    domain: str = typer.Option(..., envvar="IGN8_DOMAIN", help="Mail domain"),
    imap_host: str = typer.Option("", help="IMAP hostname (default: mail.<domain>)"),
    port: int = typer.Option(8000, help="Local port to serve on"),
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
) -> None:
    """Start the webmail interface in a local browser."""
    host = imap_host or f"mail.{domain}"

    if not _vault.exists():
        console.print("[red]Error:[/red] Vault not found — run `ign8mail vault-init` first.")
        raise typer.Exit(1)

    vault_data = vault.load(_vault)
    passwords: dict[str, str] = vault_data.get("mailbox_passwords") or {}
    autocreated: dict = vault_data.get("autocreated_mailboxes") or {}

    if not passwords:
        console.print("[yellow]No mailbox passwords in vault — add mailboxes first.[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"Starting webmail on [bold]http://127.0.0.1:{port}[/bold] — press Ctrl+C to stop."
    )

    from ign8mail.webmail.runner import start
    start(
        domain=domain,
        imap_host=host,
        passwords=passwords,
        autocreated=autocreated,
        work_dir=work_dir,
        vault_path=_vault,
        port=port,
    )


def _check_mail(host: str, username: str, password: str, domain: str) -> dict:
    """Send a probe email via SMTP and verify delivery via IMAP. Returns a result dict."""
    import imaplib
    import smtplib
    import ssl
    import time
    import uuid
    from email.message import EmailMessage

    marker = f"ign8mail-test-{uuid.uuid4().hex[:8]}"
    address = f"{username}@{domain}"
    result: dict = {"send_ok": False, "receive_ok": False, "latency_s": None, "error": None}

    try:
        msg = EmailMessage()
        msg["From"] = address
        msg["To"] = address
        msg["Subject"] = f"ign8mail delivery test {marker}"
        msg.set_content("Automated delivery probe — safe to delete.")

        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, 587, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.login(username, password)
            smtp.send_message(msg)
        result["send_ok"] = True
    except Exception as exc:
        result["error"] = f"SMTP: {exc}"
        return result

    # Poll IMAP until the message appears (up to ~20 s).
    sent_at = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        with imaplib.IMAP4_SSL(host, 993, ssl_context=ctx) as imap:
            imap.login(username, password)
            imap.select("INBOX")
            for _ in range(7):
                _, data = imap.search(None, "SUBJECT", f'"ign8mail delivery test {marker}"')
                ids = data[0].split()
                if ids:
                    result["latency_s"] = round(time.monotonic() - sent_at, 1)
                    result["receive_ok"] = True
                    imap.store(ids[-1], "+FLAGS", "\\Deleted")
                    imap.expunge()
                    break
                time.sleep(3)
        if not result["receive_ok"]:
            result["error"] = "IMAP: test message not found after 20 s"
    except Exception as exc:
        result["error"] = f"IMAP: {exc}"

    return result


def _print_status_pretty(info: dict) -> None:
    provisioned = info["provisioned"]
    state_str = "[green]provisioned[/green]" if provisioned else "[yellow]not provisioned[/yellow]"

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("key", style="bold")
    table.add_column("value")

    table.add_row("State", state_str)
    table.add_row("Keys", "[green]present[/green]" if info["keys_present"] else "[yellow]missing[/yellow]")

    if info["ipv4"]:
        table.add_row("IPv4", info["ipv4"])
    if info["ipv6"]:
        table.add_row("IPv6", info["ipv6"])
    if info["server_id"]:
        table.add_row("Server ID", str(info["server_id"]))

    server_status = info.get("server_status")
    if server_status is not None:
        color = "green" if server_status == "running" else "red"
        table.add_row("Hetzner status", f"[{color}]{server_status}[/{color}]")

    services = info.get("services")
    if services:
        for svc, state in services.items():
            if svc == "error":
                table.add_row("Services", f"[red]SSH error: {state}[/red]")
            else:
                color = "green" if state == "active" else "red"
                table.add_row(f"  {svc}", f"[{color}]{state}[/{color}]")

    mail_test = info.get("mail_test")
    if mail_test:
        send_ok = mail_test.get("send_ok")
        recv_ok = mail_test.get("receive_ok")
        latency = mail_test.get("latency_s")
        error = mail_test.get("error")
        send_str = "[green]ok[/green]" if send_ok else "[red]failed[/red]"
        if recv_ok:
            recv_str = f"[green]ok[/green] ({latency}s)"
        else:
            recv_str = "[red]failed[/red]"
        table.add_row("Mail send", send_str)
        table.add_row("Mail receive", recv_str)
        if error:
            table.add_row("  error", f"[red]{error}[/red]")

    console.print(Panel(table, title="[bold]ign8mail status[/bold]"))


def _save_backup(work_dir: Path, hetzner_token: str, backup_path: Path) -> None:
    """SSH into the running server and write backup.json. Warns but does not raise on failure."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[yellow]No state file — cannot create backup.[/yellow]")
        return

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not (ipv4 and priv_key.exists()):
        console.print("[yellow]Cannot SSH for backup (no IP or key found).[/yellow]")
        return

    console.print("Backing up server data...")
    try:
        cfg = Config(
            domain="placeholder",
            admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
            hetzner_token=hetzner_token,
            cloudflare_token="placeholder",
            cloudflare_zone_id="placeholder",
        )
        setup = MailServerSetup(cfg, host=ipv4, private_key_path=str(priv_key))
        setup.connect(retries=3, delay=5)
        bk = setup.backup()
        setup.disconnect()

        users = bk.get("users", [])
        if users:
            first_user = users[0]["username"]
            bk["local_mail_hint"] = str(Path.home() / "Mail" / first_user)

        backup_path.write_text(json.dumps(bk, indent=2))
        console.print(f"    Backup saved to [cyan]{backup_path}[/cyan]")
    except Exception as exc:
        console.print(f"    [yellow]Warning: backup failed ({exc}).[/yellow]")


@app.command()
def ssh(
    work_dir: Path = typer.Option(Path(".ign8mail"), help="Directory used during `up`"),
    user: str = typer.Option("root", help="Remote user to connect as"),
) -> None:
    """Open an interactive SSH session to the provisioned server."""
    state_path = work_dir / "state.json"
    if not state_path.exists():
        console.print("[red]Error:[/red] No state file found — run `ign8mail up` first.")
        raise typer.Exit(1)

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"

    if not ipv4:
        console.print("[red]Error:[/red] No server IP in state file.")
        raise typer.Exit(1)
    if not priv_key.exists():
        console.print(f"[red]Error:[/red] SSH key not found at {priv_key}.")
        raise typer.Exit(1)

    os.execvp("ssh", ["ssh", "-i", str(priv_key), "-o", "StrictHostKeyChecking=accept-new", f"{user}@{ipv4}"])


def _mbsync_write_profile(path: Path, username: str, password: str, imap_host: str, maildir: Path) -> None:
    content = (
        f"IMAPAccount {username}\n"
        f"Host {imap_host}\n"
        f"Port 993\n"
        f"User {username}\n"
        f"Pass {password}\n"
        f"TLSType IMAPS\n"
        f"\n"
        f"IMAPStore {username}-remote\n"
        f"Account {username}\n"
        f"\n"
        f"MaildirStore {username}-local\n"
        f"SubFolders Verbatim\n"
        f"Path {maildir}/\n"
        f"Inbox {maildir}/Inbox\n"
        f"\n"
        f"Channel {username}\n"
        f"Far :{username}-remote:\n"
        f"Near :{username}-local:\n"
        f"Patterns *\n"
        f"Create Near\n"
        f"Expunge Near\n"
        f"SyncState *\n"
    )
    path.write_text(content)
    path.chmod(0o600)


def _mbsync_switch_symlink(mbsyncrc: Path, username: str) -> None:
    profile = mbsyncrc.parent / f".mbsync.{username}"
    if mbsyncrc.is_symlink() or mbsyncrc.exists():
        mbsyncrc.unlink()
    mbsyncrc.symlink_to(profile.name)


def _msmtp_update_password(path: Path, username: str, password: str) -> None:
    if not path.exists():
        return
    in_account = False
    lines = path.read_text().splitlines(keepends=True)
    result = []
    for line in lines:
        if re.match(rf"^account\s+{re.escape(username)}\s*$", line):
            in_account = True
        elif re.match(r"^account\s+", line):
            in_account = False
        if in_account and re.match(r"^password\s+", line):
            line = f"password       {password}\n"
        result.append(line)
    path.write_text("".join(result))


def _mbsync_has_account(path: Path, username: str) -> bool:
    pattern = rf"^IMAPAccount {re.escape(username)}\s*$"
    return path.exists() and bool(re.search(pattern, path.read_text(), re.MULTILINE))


def _mbsync_add_account(
    path: Path, username: str, password: str, imap_host: str, maildir: Path
) -> None:
    block = (
        f"\nIMAPAccount {username}\n"
        f"Host {imap_host}\n"
        f"Port 993\n"
        f"User {username}\n"
        f"Pass {password}\n"
        f"TLSType IMAPS\n"
        f"\n"
        f"IMAPStore {username}-remote\n"
        f"Account {username}\n"
        f"\n"
        f"MaildirStore {username}-local\n"
        f"SubFolders Verbatim\n"
        f"Path {maildir}/\n"
        f"Inbox {maildir}/Inbox\n"
        f"\n"
        f"Channel {username}\n"
        f"Far :{username}-remote:\n"
        f"Near :{username}-local:\n"
        f"Patterns *\n"
        f"Create Near\n"
        f"Expunge Near\n"
        f"SyncState *\n"
    )
    with path.open("a") as f:
        f.write(block)
    path.chmod(0o600)


def _msmtp_has_account(path: Path, username: str) -> bool:
    pattern = rf"^account\s+{re.escape(username)}\s*$"
    return path.exists() and bool(re.search(pattern, path.read_text(), re.MULTILINE))


def _msmtp_add_account(
    path: Path, username: str, email: str, password: str, smtp_host: str
) -> None:
    defaults = (
        "defaults\n"
        "auth           on\n"
        "tls            on\n"
        "tls_starttls   on\n"
        "tls_trust_file /etc/ssl/cert.pem\n"
        "logfile        ~/.msmtp.log\n"
    )
    block = (
        f"\naccount        {username}\n"
        f"host           {smtp_host}\n"
        f"port           587\n"
        f"from           {email}\n"
        f"user           {username}\n"
        f"password       {password}\n"
    )
    if not path.exists():
        path.write_text(defaults + block)
    else:
        with path.open("a") as f:
            f.write(block)
    path.chmod(0o600)


def _msmtp_set_default(path: Path, username: str) -> None:
    if not path.exists():
        return
    text = path.read_text()
    if re.search(r"^account default:", text, re.MULTILINE):
        text = re.sub(
            r"^account default:.*$", f"account default: {username}", text, flags=re.MULTILINE
        )
    else:
        text = text.rstrip("\n") + f"\naccount default: {username}\n"
    path.write_text(text)


def _neomutt_switch(path: Path, username: str, email: str, maildir: Path) -> None:
    def _set(text: str, key: str, value: str) -> str:
        pattern = rf"^set {re.escape(key)}\s*=.*$"
        repl = f'set {key:<12}= "{value}"'
        result, n = re.subn(pattern, repl, text, flags=re.MULTILINE)
        if n == 0:
            result = result.rstrip("\n") + f'\nset {key:<12}= "{value}"\n'
        return result

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'set realname  = "{username}"\n'
            f'set from      = "{email}"\n'
            f'set mbox_type = Maildir\n'
            f'set folder    = "{maildir}"\n'
            f'set spoolfile = "+Inbox"\n'
            f'set record    = "+Sent"\n'
            f'set postponed = "+Drafts"\n'
            f'set trash     = "+Trash"\n'
        )
        return

    text = path.read_text()
    for key, value in (("realname", username), ("from", email), ("folder", str(maildir))):
        text = _set(text, key, value)
    path.write_text(text)


def _openssh_pub_to_pem(pub_path: Path) -> str:
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key, Encoding, PublicFormat
    pub = load_ssh_public_key(pub_path.read_bytes())
    return pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


def main() -> None:
    app()
