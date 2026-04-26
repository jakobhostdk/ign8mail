#!/usr/bin/env bash
# Sets up neomutt + mbsync + msmtp on macOS for the ign8mail server.
# Installs packages via Homebrew, writes config files, and does an initial sync.

set -euo pipefail

IMAP_HOST="mail.jakobholst.dk"
IMAP_PORT=993
SMTP_HOST="mail.jakobholst.dk"
SMTP_PORT=587

# ── Collect credentials ────────────────────────────────────────────────────────

echo "=== ign8mail client setup ==="
echo

if [[ -z "${EMAIL:-}" ]]; then
  read -rp "Email address: " EMAIL
fi
if [[ -z "${PASSWORD:-}" ]]; then
  read -rsp "Password: " PASSWORD
  echo
fi
echo

USERNAME="${EMAIL%%@*}"   # part before @ — used as display name / maildir label
LINUXUSER="${LINUX_USER:-$USERNAME}"  # override with LINUX_USER= if different from email prefix

# ── Install packages ──────────────────────────────────────────────────────────

echo "→ Installing neomutt, isync, msmtp..."
brew install neomutt isync msmtp

# ── Maildir ───────────────────────────────────────────────────────────────────

MAILDIR="$HOME/Mail/$USERNAME"
mkdir -p "$MAILDIR"

# ── mbsync config (~/.mbsyncrc) ───────────────────────────────────────────────

MBSYNCRC="$HOME/.mbsyncrc"
echo "→ Writing $MBSYNCRC"
cat > "$MBSYNCRC" <<EOF
IMAPAccount $USERNAME
Host $IMAP_HOST
Port $IMAP_PORT
User $LINUXUSER
Pass $PASSWORD
TLSType IMAPS

IMAPStore ${USERNAME}-remote
Account $USERNAME

MaildirStore ${USERNAME}-local
SubFolders Verbatim
Path $MAILDIR/
Inbox $MAILDIR/Inbox

Channel $USERNAME
Far :${USERNAME}-remote:
Near :${USERNAME}-local:
Patterns *
Create Near
Expunge Near
SyncState *
EOF
chmod 600 "$MBSYNCRC"

# ── msmtp config (~/.msmtprc) ─────────────────────────────────────────────────

MSMTPRC="$HOME/.msmtprc"
echo "→ Writing $MSMTPRC"
cat > "$MSMTPRC" <<EOF
defaults
auth           on
tls            on
tls_starttls   on
tls_trust_file /etc/ssl/cert.pem
logfile        ~/.msmtp.log

account        $USERNAME
host           $SMTP_HOST
port           $SMTP_PORT
from           $EMAIL
user           $LINUXUSER
password       $PASSWORD

account default: $USERNAME
EOF
chmod 600 "$MSMTPRC"

# ── neomutt config (~/.config/neomutt/neomuttrc) ──────────────────────────────

NEOMUTTDIR="$HOME/.config/neomutt"
mkdir -p "$NEOMUTTDIR"
NEOMUTTRC="$NEOMUTTDIR/neomuttrc"
echo "→ Writing $NEOMUTTRC"
cat > "$NEOMUTTRC" <<EOF
# Identity
set realname  = "$USERNAME"
set from      = "$EMAIL"

# Local maildir (synced by mbsync)
set mbox_type = Maildir
set folder    = "$MAILDIR"
set spoolfile = "+Inbox"
set record    = "+Sent"
set postponed = "+Drafts"
set trash     = "+Trash"

# Sending via msmtp
set sendmail = "$(which msmtp)"
set sendmail_wait = 0

# Sidebar
set sidebar_visible   = yes
set sidebar_width     = 25
set sidebar_format    = "%B%?F? [%F]?%* %?N?%N/?%S"
mailboxes =Inbox =Sent =Drafts =Trash =Spam

# Key bindings
bind index,pager B sidebar-toggle-visible
bind index,pager \CP sidebar-prev
bind index,pager \CN sidebar-next
bind index,pager \CO sidebar-open
bind index G last-entry
bind index gg first-entry
bind pager j next-line
bind pager k previous-line
bind pager gg top
bind pager G bottom

# Sane defaults
set sort            = threads
set sort_aux        = reverse-last-date-received
set mark_old        = no
set menu_scroll     = yes
set pager_stop      = yes
set smart_wrap      = yes
set date_format     = "%d %b %Y %H:%M"
set index_format    = "%4C  %Z  %-20.20F  %s  (%D)"
set editor          = `echo \${VISUAL:-\${EDITOR:-vim}}`
auto_view text/html
alternative_order text/plain text/html
EOF

# ── Initial sync ──────────────────────────────────────────────────────────────

echo
echo "→ Running initial mbsync (this may take a moment)..."
mbsync -a

# ── Done ──────────────────────────────────────────────────────────────────────

echo
echo "=== Done! ==="
echo
echo "Run 'neomutt' to open your mail."
echo "Sync new mail any time with: mbsync -a"
echo
echo "Tip: add this to your crontab to sync every 5 minutes:"
echo "  */5 * * * * mbsync -a -q"
