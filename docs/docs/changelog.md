# Changelog

## 2.6.0 — 2026-10-28

First PyPI release since 2.3.0. Bundles the cooperative-webmail work from the
2.4 / 2.5 git line (never published) plus the 2.6.0 additions.

### CLI

- **`save` / `load`** — snapshot and restore the entire `.ign8mail/` state dir
  (Ansible vault, SSH + DKIM keys, Pulumi state, autocreated-mailbox metadata)
  as a tar.gz. Session caches (`seen.db`, `tui_archived.json`) and
  `__pycache__` are excluded automatically.
- **`passwd-reset <user>`** — rotate a single mailbox password on the server,
  Ansible vault, mbsync profile, msmtp config, and aerc config in one call.
- **`install-tb-autoconfig` + `reset-passwords`** — feed Thunderbird every
  active mailbox + vault password via a privileged autoconfig that calls
  `Services.logins.addLogin()` directly (same code path TB's own UI uses).

### Webmail

- **Async sidebar + inbox** — the heavy IMAP calls moved out of the initial
  render into new `/api/sidebar/` and `/api/inbox/` endpoints. First paint is
  now near-instant; mailbox unread counts stream in client-side.
- **Longer timeouts** — gunicorn systemd unit bumped to `--timeout 120`;
  nginx proxy to `180s` send/read — fixes 504s on large mailboxes.

### Housekeeping

- `.gitignore` now excludes `.ign8mail/` (vault + keys) and `.claude/`.

### Carried forward from unpublished 2.4 / 2.5

- Cooperative webmail with per-user identity (`ign8mail user-add`, shared
  mailboxes)
- `deploy-webmail` serves the webmail at `webmail.<domain>` behind Cloudflare
- Threaded inbox with swimlane timeline, mailbox context switcher,
  attachment download, compose with attachments, sticky 90-day window
- `autocreated enable` gates webmail visibility
- `sync-claude` registers mailboxes as Claude MCP servers
- MkDocs Material docs site under `docs/`

[Full changelog on GitHub](https://github.com/jakobhostdk/ign8mail/compare/v2.2.1...v2.6.0)
