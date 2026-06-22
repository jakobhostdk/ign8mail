# CLI reference

All commands accept `--help` for full flag listings. This page summarises every command grouped by purpose.

## Lifecycle

### `ign8mail up`

Provision a complete mail server: Hetzner VM + Cloudflare DNS + Postfix/Dovecot.

```bash
ign8mail up \
  --domain example.com \
  --admin-email admin@example.com \
  --hetzner-token $IGN8_HETZNER_TOKEN \
  --cloudflare-token $IGN8_CLOUDFLARE_TOKEN \
  --cloudflare-zone-id $IGN8_CLOUDFLARE_ZONE_ID
```

Idempotent — safe to re-run.

### `ign8mail destroy`

Tear down the Hetzner server, saving a backup first. DNS records are left intact.

### `ign8mail backup`

Snapshot server state (users, config) to `backup.json` without touching the server.

### `ign8mail status`

Show the current state of the provisioned mail server.

```bash
ign8mail status --check-services        # SSH in, verify postfix/dovecot/rspamd
ign8mail status --check-mail            # Send a probe email and verify IMAP delivery
ign8mail status --format json
```

## Mailbox management

### `ign8mail create-mailbox <name>`

Create a mailbox on the running server. Generates a password and stores it in the vault.

### `ign8mail delete-mailbox <name>`

Delete a mailbox. Use `--purge` to also remove the home directory and all mail.

### `ign8mail adduser <username>`

Create a mail user (lower-level alternative to `create-mailbox`).

### `ign8mail list`

List known mail inboxes on the running server.

### `ign8mail list-new`

Show new (unread) mail across every mailbox, then open the reader TUI. Use `--no-tui` for a static table.

### `ign8mail show-passwords`

Print every mailbox password from the vault.

### `ign8mail passwd-check`

Probe IMAP login for every account in the vault. Non-destructive.

### `ign8mail passwd-recycle`

Reset all mail account passwords and rewrite per-mailbox `~/.mbsync.<user>` profiles.

## Reading mail

### `ign8mail read [account]`

Open the multi-account TUI reader. Optionally pass an account to focus on launch.

### `ign8mail webmail`

Start the bundled Django webmail interface on `localhost:8000`.

### `ign8mail switch-inbox <account>`

Switch local `mbsync`/`neomutt`/`msmtp` configs to point at a different mailbox.

### `ign8mail tail-postfix-log`

Stream the Postfix log from the running server.

## Client integration

### `ign8mail configure-thunderbird <username>`

Add an IMAP/SMTP account to your local Thunderbird profile.

### `ign8mail sync-thunderbird`

Add **all** active mailboxes to Thunderbird and store their passwords.

### `ign8mail sync-aerc`

Add all active mailboxes to aerc's `accounts.conf`.

## Cooperative webmail

See [Cooperative webmail](webmail.md) for the full model.

### `ign8mail user-add <username> [-p <address>]`

Register *username* as a webmail user. `--personal/-p` is repeatable; default is `<username>@<domain>`.

### `ign8mail configure-sharing`

Push `smtpd_sender_login_maps` to the server and turn on `reject_sender_login_mismatch` on submission (port 587). Re-run after changing `users:` in the vault.

## Server-side extras

### `ign8mail configure-nginx`

Deploy the landing page and configure nginx on the running server.

### `ign8mail configure-dkim`

Upload the DKIM key and configure rspamd signing.

### `ign8mail deploy-webmail`

Deploy the webmail interface to `webmail.<domain>` via Cloudflare proxy.

### `ign8mail enable-autocreate`

Deploy the autocreate policy daemon — accept mail for unknown local-parts and create mailboxes on demand.

### `ign8mail seed`

Send a seed email to each address in `mailboxes.yml` so the autocreate daemon provisions them.

## Auto-created mailboxes

`ign8mail autocreated` — manage mailboxes created on-the-fly by the autocreate daemon.

| Subcommand | Purpose |
|---|---|
| `autocreated list` | List auto-created mailboxes and their status |
| `autocreated sync` | Pull new auto-created mailboxes into the vault |
| `autocreated enable <name>` | Mark mailbox as kept |
| `autocreated disable <name>` | Stop accepting new mail (existing mail kept) |
| `autocreated silence <name>` | Delete mailbox and block future creation |

## DMARC

### `ign8mail dmarc-stats`

Fetch and display DMARC aggregate report statistics.

```bash
ign8mail dmarc-stats --delete   # also delete parsed report emails
```

## Vault & SSH

### `ign8mail vault-init`

Create `.ign8mail/vault.yml` from current `IGN8_*` environment variables.

### `ign8mail ssh`

Open an interactive SSH session to the provisioned server.

### `ign8mail checkmail --mail-user <user>`

Send a test email and verify IMAP delivery for a single mailbox. Use `--fix` to attempt repairs.
