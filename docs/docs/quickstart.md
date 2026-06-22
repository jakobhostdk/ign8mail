# Quickstart

From zero to a fully provisioned mail server in three steps.

## 1. Prerequisites

You'll need:

- **Python 3.11+**
- A **Hetzner Cloud** account with a project-scoped API token
- A domain whose DNS is managed by **Cloudflare**, plus:
    - An API Token with `Zone.DNS:Edit` scope (recommended), or a Global API Key
    - The Zone ID for that domain

## 2. Install

```bash
pipx install ign8mail
```

Or for a normal venv:

```bash
pip install ign8mail
```

## 3. Configure

Create a `.env` in the directory you want state and keys to live in:

```bash title=".env"
IGN8_DOMAIN=example.com
IGN8_ADMIN_EMAIL=admin@example.com
IGN8_HETZNER_TOKEN=hetzner_xxx
IGN8_CLOUDFLARE_TOKEN=cf_xxx
IGN8_CLOUDFLARE_ZONE_ID=abcdef0123456789
```

!!! tip "API Token vs Global API Key"
    If you use a Cloudflare **API Token** (recommended), leave `IGN8_CLOUDFLARE_EMAIL` unset.
    If you use the **Global API Key**, also set `IGN8_CLOUDFLARE_EMAIL=you@example.com`.

## 4. Ignite

```bash
ign8mail up
```

Expect the run to take 3–5 minutes. The CLI prints progress for each phase:

1. SSH + DKIM keypair generation
2. Hetzner VM + firewall provisioning
3. Cloudflare DNS records
4. SSH-driven install + configuration of Postfix, Dovecot, rspamd, certbot

When it finishes, your mail server is reachable on `mail.<your-domain>`.

## 5. Add a mailbox

```bash
ign8mail create-mailbox alice
```

The password is generated, stored in `.ign8mail/vault.yml`, and printed once.

## 6. Read mail

The bundled TUI reader:

```bash
ign8mail read              # all accounts
ign8mail list-new          # show unread across every mailbox, then open the reader
```

Or wire it into Thunderbird / aerc / neomutt — see [Operations](operations.md).

## 7. Tear down

```bash
ign8mail destroy
```

Removes the Hetzner server. DNS records are left intact so you can re-provision without DNS propagation delay.
