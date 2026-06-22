# ign8mail

> Ignite a production-grade mail server in minutes.

`ign8mail` is a single-command Python CLI that provisions a complete, working mail server from scratch — VM, DNS, TLS, and mail stack all wired together.

## What it does in one command

1. **Generates an SSH keypair** (and a DKIM keypair) into `.ign8mail/keys/`.
2. **Provisions a Hetzner Cloud VPS** via the `hcloud` SDK — server, firewall, SSH key.
3. **Creates all required Cloudflare DNS records** — A, AAAA, MX, SPF, DKIM, DMARC, autoconfig/autodiscover.
4. **SSHs into the server** and installs + configures Postfix, Dovecot, rspamd, and a Let's Encrypt certificate via certbot.

After `ign8mail up` returns, you have a working mail server with TLS, DKIM signing, SPF, DMARC, and IMAPS/SMTPS reachable on `mail.<domain>`.

## Backend stack

| Component | Role |
|-----------|------|
| **Postfix** | SMTP MTA (incoming on 25, submission on 587, SMTPS on 465) |
| **Dovecot** | IMAP server (IMAPS on 993) and SASL auth backend |
| **rspamd** | Spam filtering and DKIM signing |
| **certbot** | Let's Encrypt TLS certificates |
| **Hetzner Cloud** | VPS host (Ubuntu 24.04, default `cx23` in `nbg1`) |
| **Cloudflare** | DNS provider |

## Install

```bash
pip install ign8mail
# or
pipx install ign8mail
```

## Next steps

- [Quickstart](quickstart.md) — your first mail server in five minutes.
- [Cooperative webmail](webmail.md) — share `support@`, `sales@` etc. across the team; keep `<username>@` personal.
- [Architecture](architecture.md) — what gets provisioned and where state lives.
- [CLI reference](cli.md) — every command and flag.
- [DNS records](dns.md) — exactly which records get created.
- [Configuration](configuration.md) — all `IGN8_`-prefixed env vars.
- [Operations](operations.md) — day-2 tasks: backup, destroy, mailbox management, TUI reader.
