# Cooperative webmail

`ign8mail`'s webmail treats your domain as a single shared workspace. Anyone with an account can read every non-personal mailbox in `mailbox_passwords` — perfect for `support@`, `sales@`, `info@`-style addresses that the whole team handles together. Each user's `<username>@<domain>` (and any extra addresses they claim) stays 100% private.

## The model

| Concept | Behavior |
|---|---|
| **Personal mailbox** | Listed in a user's `personal_addresses`. Only that user can read it or send from it. |
| **Shared mailbox** | Everything else in `mailbox_passwords`. Visible and writable to all registered users. |
| **Unified inbox** | The home page is a single time-ordered stream of every mailbox the logged-in user can see, with a mailbox-tag on each row. |
| **Per-user read state** | Reading a shared message marks it seen *for you only* (SQLite overlay). Others still see it as unread. Personal mailboxes use Dovecot's `\Seen` flag normally. |
| **Send-as enforcement** | Postfix `smtpd_sender_login_maps` rejects any submission where the SASL login isn't allowed to use the From address. A leaked password cannot impersonate anyone else's personal address. |

## Setting it up

### 1. Create mailboxes

Same as before — one mailbox per address:

```bash
ign8mail create-mailbox jakob       # will become personal
ign8mail create-mailbox alice       # will become personal
ign8mail create-mailbox support     # will stay shared
ign8mail create-mailbox sales       # will stay shared
```

### 2. Register users

Declare which addresses are personal-to-whom:

```bash
ign8mail user-add jakob                              # personal = jakob@<domain>
ign8mail user-add jakob -p jakob@jakobholst.dk \
                         -p lyngknuden@jakobholst.dk # multiple personal addresses
ign8mail user-add alice                              # personal = alice@<domain>
```

Anyone with a `mailbox_passwords` entry but **no** `users` entry is treated as a shared mailbox automatically.

### 3. Push send-as enforcement to the server

```bash
ign8mail configure-sharing
```

This generates `/etc/postfix/sender_login_maps` and rewrites the submission block in `/etc/postfix/master.cf` to require `reject_sender_login_mismatch`. Port 25 (MTA-to-MTA) is untouched so external mail keeps arriving.

### 4. Start the webmail

```bash
ign8mail webmail
```

Open <http://127.0.0.1:8000>. Log in with your IMAP username + password.

## What each user sees

For a vault where:

```yaml
mailbox_passwords:
  jakob: ...
  alice: ...
  support: ...
  sales: ...
users:
  jakob:
    personal_addresses: [jakob@jakobholst.dk, lyngknuden@jakobholst.dk]
  alice:
    personal_addresses: [alice@jakobholst.dk]
```

| | Jakob sees | Alice sees |
|---|---|---|
| **Sidebar** | `jakob` 🔥, `lyngknuden` 🔥, `support` 💬, `sales` 💬 | `alice` 🔥, `support` 💬, `sales` 💬 |
| **Can read** | All four | Three (cannot see `jakob`/`lyngknuden`) |
| **Can send from** | `jakob@`, `lyngknuden@`, `support@`, `sales@` | `alice@`, `support@`, `sales@` |
| **Default From** | `jakob@jakobholst.dk` | `alice@jakobholst.dk` |

🔥 = personal · 💬 = shared

## Sharing & disabling addresses

Because shared mailboxes are real PAM users on the server with their own Maildir, you can hand out a shared address (e.g. for a one-off project), monitor it in the unified inbox, and revoke it cleanly if it gets spammed:

```bash
# From the webmail mailbox view, click "Disable" or "Silence" on a shared mailbox.
# CLI equivalent:
ign8mail autocreated disable spam-magnet
ign8mail autocreated silence spam-magnet   # also delete the maildir + blocklist
```

Personal mailboxes never expose the Disable/Silence actions in the UI.

## Compose

Hit **＋ Compose** in the top bar. From-dropdown lists exactly the addresses Postfix will accept from you — your personal addresses plus every shared one. The message is submitted via Postfix on port 587 with STARTTLS, authenticated with your own SASL credentials. `sender_login_maps` is the final authority: if you try to forge an address you're not allowed to use, Postfix rejects the message.

## Read-state overlay

`~/.ign8mail/seen.db` is a small SQLite file tracking `(user, mailbox, uid, seen_at)` for shared mailboxes. Nothing leaks to other users; Dovecot's `\Seen` flag is never set on shared messages from webmail traffic, so simultaneous clients (Thunderbird, etc.) keep seeing them as unread until they actually read them.

Personal mailboxes bypass the overlay and use Dovecot's flag directly — same behavior as a regular IMAP client.

## Limitations

- **Webmail only.** Thunderbird/aerc users log in to their own IMAP account and see only that mailbox. To get shared access for desktop clients you'd need Dovecot ACL / shared namespaces — not provisioned by `ign8mail`.
- **Per-user read state lives on the webmail host.** Don't run two `ign8mail webmail` instances on different machines — they would diverge.
- **Compose has no attachments yet.** Plain-text body only.
