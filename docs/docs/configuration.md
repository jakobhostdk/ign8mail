# Configuration

All settings come from a single `Config` (pydantic-settings) object. Values can be supplied via CLI flags, environment variables, or a `.env` file in the working directory.

## Required

| Env var | CLI flag | Description |
|---|---|---|
| `IGN8_DOMAIN` | `--domain` | Your mail domain, e.g. `example.com` (no trailing dot) |
| `IGN8_ADMIN_EMAIL` | `--admin-email` | Admin / certbot notification email |
| `IGN8_HETZNER_TOKEN` | `--hetzner-token` | Hetzner Cloud API token (project-scoped) |
| `IGN8_CLOUDFLARE_TOKEN`<br>or `IGN8_CLOUDFLARE` | `--cloudflare-token` | Cloudflare API Token (or Global API Key) |
| `IGN8_CLOUDFLARE_ZONE_ID` | `--cloudflare-zone-id` | Cloudflare Zone ID for the domain |

## Cloudflare auth flavour

| Env var | When to set |
|---|---|
| `IGN8_CLOUDFLARE_EMAIL` | Set **only** when using a Global API Key. Leave unset for API Tokens. |

## Server (Hetzner)

| Env var | Default | Description |
|---|---|---|
| `IGN8_SERVER_LOCATION` | `nbg1` | Datacenter — one of `nbg1` (Nuremberg), `fsn1` (Falkenstein), `hel1` (Helsinki), etc. |
| `IGN8_SERVER_TYPE` | `cx23` | Hetzner server type. `cx23` is a 2 vCPU / 8 GB tier well-suited to single-domain mail. |
| `IGN8_SERVER_IMAGE` | `ubuntu-24.04` | OS image |

## Mail limits

| Env var | Default | Description |
|---|---|---|
| `IGN8_MAILBOX_QUOTA_MB` | `2048` | Per-mailbox Dovecot quota (MB) |
| `IGN8_MAX_MESSAGE_SIZE_MB` | `50` | Postfix `message_size_limit` (MB) |

## Vault & state

| Env var | Default | Description |
|---|---|---|
| `IGN8_VAULT` | `.ign8mail/vault.yml` | Ansible-vault file with mailbox passwords |
| `IGN8_MAIL_PASSWORD` | — | Per-command password override (used by `read`, `checkmail`, `switch-inbox`) |
| `IGN8_WEBMAIL_PASSWORD` | — | Webmail admin password |

## `.env` example

```bash title=".env"
# --- Required ---
IGN8_DOMAIN=example.com
IGN8_ADMIN_EMAIL=admin@example.com
IGN8_HETZNER_TOKEN=hetzner_xxx
IGN8_CLOUDFLARE_TOKEN=cf_token_xxx
IGN8_CLOUDFLARE_ZONE_ID=abcdef0123456789

# --- Optional (defaults shown) ---
IGN8_SERVER_LOCATION=nbg1
IGN8_SERVER_TYPE=cx23
IGN8_SERVER_IMAGE=ubuntu-24.04
IGN8_MAILBOX_QUOTA_MB=2048
IGN8_MAX_MESSAGE_SIZE_MB=50
```

## Precedence

For any setting, the resolution order is:

1. **CLI flag** (e.g. `--domain example.com`)
2. **Environment variable** (e.g. `IGN8_DOMAIN=example.com`)
3. **`.env` file** in the current working directory
4. **Default value** (from `config.py`)

The domain is normalised — trailing dots are stripped automatically.
