# Architecture

## The four-step provisioning flow

```
ign8mail up
    │
    ├─▶ 1. keys.save_keypair()           ──▶ .ign8mail/keys/
    │      RSA SSH + DKIM keypairs
    │
    ├─▶ 2. infra.hetzner.build()         ──▶ Hetzner Cloud
    │      Server + Firewall + SshKey
    │      (hcloud SDK, no Pulumi.yaml)
    │
    ├─▶ 3. dns.cloudflare.provision()    ──▶ Cloudflare API
    │      A, AAAA, MX, SPF, DKIM,
    │      DMARC, autoconfig, autodiscover
    │
    └─▶ 4. mailserver.setup.MailServerSetup
           SSH into the server, apt install,
           configure Postfix + Dovecot + rspamd,
           run certbot for TLS
```

Each step is independent and idempotent — re-running `up` against an existing server reconciles state rather than re-creating it.

## Source layout

```
src/ign8mail/
├── cli.py            # Typer app; orchestrates the 4-step flow
├── config.py         # Pydantic-settings Config; env prefix IGN8_; reads .env
├── keys.py           # RSA keypair generation (SSH + DKIM)
├── vault.py          # Ansible-vault-encrypted mailbox secret store
├── infra/
│   └── hetzner.py    # hcloud SDK: Server + Firewall + SshKey
├── dns/
│   └── cloudflare.py # CloudflareDNS.provision() — creates all records
├── mailserver/
│   └── setup.py      # MailServerSetup: SSH, apt, postfix/dovecot, certbot
├── tui/              # Textual-based multi-account mail reader
│   ├── app.py
│   ├── client.py
│   └── newmail.py
├── webmail/          # Optional Django webmail UI
└── thunderbird.py    # Thunderbird profile sync helpers
```

## The Hetzner VM

| Setting | Default | Override |
|---------|---------|----------|
| Server type | `cx23` | `IGN8_SERVER_TYPE` |
| Image | `ubuntu-24.04` | `IGN8_SERVER_IMAGE` |
| Location | `nbg1` | `IGN8_SERVER_LOCATION` |
| Firewall name | `ign8mail-fw` | — |

### Firewall rules

Inbound TCP allowed on:

| Port | Service |
|------|---------|
| 22 | SSH |
| 25 | SMTP (MTA-to-MTA) |
| 80 | HTTP (Let's Encrypt + landing page) |
| 443 | HTTPS (landing page + webmail) |
| 465 | SMTPS (submission over implicit TLS) |
| 587 | SMTP submission with STARTTLS |
| 993 | IMAPS |

All other inbound traffic is dropped. Outbound traffic is unrestricted.

## Mail stack on the server

| Component | Role |
|-----------|------|
| **Postfix** | SMTP. Smtpd-tls-cert from Let's Encrypt, sasl auth via Dovecot |
| **Dovecot** | IMAP, Maildir-format storage under `/home/<user>/Maildir`, also SASL auth backend for Postfix |
| **rspamd** | Spam scoring + DKIM signing (selector `mail2`) |
| **certbot** | TLS certificates, auto-renewed |
| **Linux users** | Each mailbox is a real PAM user. Passwords are stored in the vault and on the system |

## State and secrets

Everything lives under `.ign8mail/` in the directory you ran `ign8mail up` from:

```
.ign8mail/
├── keys/
│   ├── id_rsa           # SSH private key (mode 0600)
│   ├── id_rsa.pub       # SSH public key (uploaded to Hetzner)
│   ├── dkim.private     # DKIM private key (uploaded to server)
│   └── dkim.public.txt  # DKIM public key (embedded in TXT record)
├── vault.yml            # Ansible-vault-encrypted mailbox passwords
└── state.json           # Server IDs, IP addresses, last-applied config
```

!!! warning "Treat `.ign8mail/` as secret"
    It contains private keys and mailbox credentials. Back it up, but never commit it.

## DNS

All DNS records are created in Cloudflare via the Python SDK in a single call to `CloudflareDNS.provision()`. See the [DNS records](dns.md) page for the exact set.
