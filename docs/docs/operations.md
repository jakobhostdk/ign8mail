# Operations

Day-2 tasks for a running `ign8mail` server.

## Creating and managing mailboxes

```bash
# Create
ign8mail create-mailbox alice

# List
ign8mail list

# Delete (keeps mail data)
ign8mail delete-mailbox alice

# Delete and wipe Maildir
ign8mail delete-mailbox alice --purge
```

Every mailbox is a real PAM user on the server. Passwords are generated, stored in `.ign8mail/vault.yml` (Ansible-vault encrypted), and printed once on creation.

## Reading mail

### TUI reader (built-in)

```bash
ign8mail read              # multi-account TUI, all mailboxes
ign8mail read alice        # open with alice focused
ign8mail list-new          # show unread across every mailbox, then drop into the reader
```

Key bindings: open the help overlay with `?` once inside the TUI.

### Webmail

```bash
ign8mail webmail           # serve on http://localhost:8000
ign8mail deploy-webmail    # deploy as webmail.<domain> via Cloudflare
```

### Thunderbird

```bash
ign8mail configure-thunderbird alice    # add one account
ign8mail sync-thunderbird               # add every active mailbox
```

### aerc / neomutt + mbsync + msmtp

```bash
ign8mail sync-aerc                      # write accounts.conf entries
ign8mail switch-inbox alice             # rewrite ~/.mbsyncrc and ~/.msmtprc
```

## Backups

```bash
ign8mail backup            # writes backup.json without touching the server
```

`backup.json` captures user list, vault, and resolved config. It does **not** snapshot Maildirs — for that, use restic or Hetzner snapshots.

`ign8mail destroy` runs a backup automatically before deleting the server.

## Health checks

```bash
ign8mail status                              # local view of state.json
ign8mail status --check-services             # SSH in, verify postfix/dovecot/rspamd
ign8mail status --check-mail \
        --mail-user alice                    # send test mail + verify IMAP receipt
ign8mail status --format json                # machine-readable
```

For a single mailbox:

```bash
ign8mail checkmail --mail-user alice
ign8mail checkmail --mail-user alice --fix   # attempt to fix missing maildir, etc.
```

## Password ops

```bash
ign8mail show-passwords     # print every mailbox password from the vault
ign8mail passwd-check       # parallel IMAP login probe against every account
ign8mail passwd-recycle     # rotate every password and rewrite mbsync profiles
```

!!! warning "Server rebuild = vault drift"
    If you rebuild the Hetzner VM out-of-band, vault passwords no longer match the server's `/etc/shadow`. `passwd-check` will report every account as failed. Fix by running `passwd-recycle` or recreating mailboxes from the vault.

## DMARC reports

```bash
ign8mail dmarc-stats                # parse and display aggregate reports
ign8mail dmarc-stats --delete       # consume reports after parsing
```

## Logs

```bash
ign8mail tail-postfix-log           # stream /var/log/mail.log
ign8mail tail-postfix-log --no-follow --log /var/log/mail.err
```

## SSH

```bash
ign8mail ssh                # interactive shell as root
ign8mail ssh --user alice   # connect as a non-root user
```

## Autocreated mailboxes

When the autocreate daemon is deployed (`ign8mail enable-autocreate`), any mail to an unknown local-part provisions a mailbox on demand.

```bash
ign8mail autocreated list           # see what was created automatically
ign8mail autocreated sync           # pull new ones into the vault
ign8mail autocreated enable bob     # promote auto-created to "kept"
ign8mail autocreated disable bob    # stop accepting new mail (keep existing)
ign8mail autocreated silence bob    # delete and blocklist future creation
```

## Tear down

```bash
ign8mail destroy
```

- Runs `backup` first.
- Deletes the Hetzner server and firewall.
- Leaves DNS records, the local `.ign8mail/` directory, and the vault untouched — so re-running `ign8mail up` reuses the same keys and DNS without propagation delay.
