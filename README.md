# ign8mail

Ignite a production-grade mail server in minutes.

**What it does in one command:**
1. Generates an SSH keypair
2. Provisions a Hetzner Cloud VPS via Pulumi
3. Creates all required Cloudflare DNS records (A, MX, SPF, DKIM, DMARC, autoconfig)
4. SSHs into the server and installs + configures Postfix, Dovecot, rspamd, and Let's Encrypt TLS

## Install

```bash
pip install ign8mail
```

## Quickstart

```bash
cp .env.example .env
# Fill in .env with your tokens and domain

ign8mail up \
  --domain mail.example.com \
  --admin-email admin@example.com \
  --hetzner-token $HETZNER_TOKEN \
  --cloudflare-token $CF_TOKEN \
  --cloudflare-zone-id $CF_ZONE_ID
```

Or use environment variables (all flags have `IGN8_` prefixed env var equivalents):

```bash
export IGN8_DOMAIN=mail.example.com
export IGN8_ADMIN_EMAIL=admin@example.com
export IGN8_HETZNER_TOKEN=...
export IGN8_CLOUDFLARE_TOKEN=...
export IGN8_CLOUDFLARE_ZONE_ID=...

ign8mail up
```

## Teardown

```bash
ign8mail destroy
```

This removes the Hetzner server. DNS records are left intact.

## Requirements

- Python 3.11+
- A Hetzner Cloud account and API token
- A domain managed by Cloudflare (API token + Zone ID)
