# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`ign8mail` is a Python CLI package that provisions a complete mail server from scratch:
- **Hetzner Cloud** — VPS created via the Pulumi Automation API (inline program, no separate `Pulumi.yaml`)
- **Cloudflare** — all DNS records (A, AAAA, MX, SPF, DKIM, DMARC, autoconfig) created via the Cloudflare Python SDK
- **Mail stack** — Postfix + Dovecot + rspamd + Let's Encrypt TLS, configured over SSH with paramiko

## Commands

```bash
# Install in editable mode (development)
pip install -e ".[dev]"

# Run CLI
ign8mail up      # full provision
ign8mail destroy # tear down server

# Lint
ruff check src tests
ruff format src tests

# Type check
mypy src

# Tests
pytest
pytest tests/test_config.py   # single file
```

## Architecture

```
src/ign8mail/
├── cli.py            # Typer app; orchestrates the 4-step flow
├── config.py         # Pydantic-settings Config; env prefix IGN8_; reads .env
├── keys.py           # RSA keypair generation (SSH + DKIM)
├── infra/
│   └── hetzner.py    # Pulumi inline program: Server + Firewall + SshKey
├── dns/
│   └── cloudflare.py # CloudflareDNS.provision() — creates all records in one call
└── mailserver/
    └── setup.py      # MailServerSetup — SSH in, apt install, configure postfix/dovecot, certbot
```

### Key flows

**`ign8mail up`** (cli.py):
1. `keys.save_keypair()` → SSH keypair written to `.ign8mail/keys/`
2. `_pulumi_up()` → Pulumi Automation API runs `hetzner.build()`, returns `ipv4`/`ipv6` outputs
3. `CloudflareDNS.provision()` → creates all DNS records; DKIM key generated separately in `keys/dkim`
4. `MailServerSetup.connect()` + `.run()` → SSH, apt, postfix/dovecot config, certbot

**Config** is a single `Config` (pydantic-settings) object passed through the whole call chain. All env vars use the `IGN8_` prefix.

**Pulumi state** is stored locally under `.ign8mail/` (passed as `work_dir`). Stack name is always `prod`.

## DNS records provisioned

MX, A, AAAA for `mail.<domain>`, SPF, DKIM (`mail._domainkey`), DMARC, autoconfig/autodiscover CNAMEs.

## Environment / credentials

Copy `.env.example` → `.env`. Required vars: `IGN8_DOMAIN`, `IGN8_ADMIN_EMAIL`, `IGN8_HETZNER_TOKEN`, `IGN8_CLOUDFLARE_TOKEN`, `IGN8_CLOUDFLARE_ZONE_ID`.
