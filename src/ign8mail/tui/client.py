"""Persistent IMAP + SMTP client for the TUI mail reader."""

from __future__ import annotations

import email as _email
import email.header
import email.utils
import imaplib
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass
class Folder:
    name: str           # raw IMAP mailbox name (UTF-7 modified)
    display: str        # decoded for the UI
    flags: tuple[str, ...]


@dataclass
class MessageSummary:
    uid: str
    subject: str
    sender: str
    date: str
    seen: bool
    answered: bool


@dataclass
class FullMessage:
    uid: str
    subject: str
    sender: str
    to: str
    cc: str
    date: str
    message_id: str
    references: str
    body_text: str
    body_html: str


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _decode_mailbox_name(raw: bytes) -> str:
    # IMAP uses modified UTF-7 for mailbox names; fall back to latin-1.
    try:
        return raw.decode("utf-7").replace("&", "&").replace("&-", "&")
    except Exception:
        return raw.decode("latin-1", errors="replace")


class MailClient:
    """Wraps a single IMAP + on-demand SMTP connection for the TUI."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        email_addr: str,
        smtp_host: str = "",
        smtp_port: int = 587,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.email = email_addr
        self.smtp_host = smtp_host or host
        self.smtp_port = smtp_port
        self._imap: imaplib.IMAP4_SSL | None = None
        self._selected: str | None = None

    # ---- connection -----------------------------------------------------

    def connect(self) -> None:
        ctx = ssl.create_default_context()
        imap = imaplib.IMAP4_SSL(self.host, 993, ssl_context=ctx)
        imap.login(self.username, self.password)
        self._imap = imap

    def close(self) -> None:
        if self._imap is not None:
            try:
                if self._selected:
                    self._imap.close()
                self._imap.logout()
            except Exception:
                pass
            self._imap = None
            self._selected = None

    def _conn(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            self.connect()
        assert self._imap is not None
        return self._imap

    # ---- folders --------------------------------------------------------

    def folders(self) -> list[Folder]:
        typ, data = self._conn().list()
        if typ != "OK" or not data:
            return []
        out: list[Folder] = []
        for line in data:
            if line is None:
                continue
            raw = line if isinstance(line, bytes) else bytes(line)
            # Format: (\HasNoChildren) "/" "INBOX"
            try:
                text = raw.decode("latin-1", errors="replace")
                flags_part, _, rest = text.partition(") ")
                flags = tuple(flags_part.lstrip("(").split())
                # rest is: "/" "MailboxName"   (delimiter then name)
                # split from the right on space to get the name
                _, _, name_part = rest.partition(" ")
                name = name_part.strip().strip('"')
                if not name:
                    continue
                out.append(Folder(name=name, display=_decode_mailbox_name(name.encode("latin-1")), flags=flags))
            except Exception:
                continue
        # INBOX first, then alpha
        out.sort(key=lambda f: (f.display.upper() != "INBOX", f.display.upper()))
        return out

    def select(self, folder: str, readonly: bool = False) -> int:
        """Select folder, return message count."""
        typ, data = self._conn().select(f'"{folder}"', readonly=readonly)
        if typ != "OK":
            self._selected = None
            return 0
        self._selected = folder
        try:
            return int(data[0])
        except Exception:
            return 0

    # ---- listing --------------------------------------------------------

    def list_messages(self, folder: str, limit: int = 200, unseen_only: bool = False) -> list[MessageSummary]:
        self.select(folder, readonly=False)
        imap = self._conn()
        criterion = "UNSEEN" if unseen_only else "ALL"
        typ, data = imap.uid("SEARCH", None, criterion)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        recent = uids[-limit:][::-1]
        if not recent:
            return []
        uid_set = b",".join(recent)
        typ, fetched = imap.uid(
            "FETCH",
            uid_set.decode(),
            "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
        )
        if typ != "OK" or not fetched:
            return []
        by_uid: dict[str, MessageSummary] = {}
        current_uid: str | None = None
        current_flags = ""
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2:
                meta, hdr_bytes = item[0], item[1]
                meta_str = meta.decode("latin-1", errors="replace") if isinstance(meta, bytes) else str(meta)
                uid = _extract_uid(meta_str)
                flags = _extract_flags(meta_str)
                if not uid:
                    continue
                msg = _email.message_from_bytes(hdr_bytes if isinstance(hdr_bytes, bytes) else b"")
                by_uid[uid] = MessageSummary(
                    uid=uid,
                    subject=_decode_header(msg.get("Subject")) or "(no subject)",
                    sender=_decode_header(msg.get("From")) or "",
                    date=msg.get("Date") or "",
                    seen="\\Seen" in flags,
                    answered="\\Answered" in flags,
                )
                current_uid = uid
                current_flags = flags
        # preserve the newest-first order
        ordered: list[MessageSummary] = []
        for uid_bytes in recent:
            uid = uid_bytes.decode()
            if uid in by_uid:
                ordered.append(by_uid[uid])
        return ordered

    # ---- single message -------------------------------------------------

    def fetch(self, folder: str, uid: str) -> FullMessage | None:
        self.select(folder)
        imap = self._conn()
        typ, data = imap.uid("FETCH", uid, "(FLAGS RFC822)")
        if typ != "OK" or not data or not data[0]:
            return None
        item = data[0]
        if not isinstance(item, tuple) or len(item) < 2:
            return None
        raw_msg = item[1] if isinstance(item[1], bytes) else b""
        msg = _email.message_from_bytes(raw_msg)
        body_text = body_html = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if part.get_content_disposition() == "attachment":
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except LookupError:
                    decoded = payload.decode("utf-8", errors="replace")
                if ct == "text/plain" and not body_text:
                    body_text = decoded
                elif ct == "text/html" and not body_html:
                    body_html = decoded
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except LookupError:
                    decoded = payload.decode("utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    body_html = decoded
                else:
                    body_text = decoded
        return FullMessage(
            uid=uid,
            subject=_decode_header(msg.get("Subject")) or "(no subject)",
            sender=_decode_header(msg.get("From")) or "",
            to=_decode_header(msg.get("To")) or "",
            cc=_decode_header(msg.get("Cc")) or "",
            date=msg.get("Date") or "",
            message_id=msg.get("Message-ID") or "",
            references=msg.get("References") or msg.get("In-Reply-To") or "",
            body_text=body_text,
            body_html=body_html,
        )

    # ---- flags + moves --------------------------------------------------

    def mark(self, folder: str, uid: str, flag: str, add: bool = True) -> None:
        self.select(folder)
        op = "+FLAGS" if add else "-FLAGS"
        self._conn().uid("STORE", uid, op, flag)

    def move(self, folder: str, uid: str, target: str) -> bool:
        self.select(folder)
        imap = self._conn()
        # Try MOVE extension first; fall back to COPY+delete
        try:
            typ, _ = imap.uid("MOVE", uid, f'"{target}"')
            if typ == "OK":
                return True
        except Exception:
            pass
        typ, _ = imap.uid("COPY", uid, f'"{target}"')
        if typ != "OK":
            return False
        imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
        imap.expunge()
        return True

    def append(self, folder: str, raw_message: bytes, flags: str = "(\\Seen)") -> bool:
        typ, _ = self._conn().append(f'"{folder}"', flags, None, raw_message)
        return typ == "OK"

    def find_special(self, attr: str, fallback: str) -> str:
        """Return the mailbox tagged with `\\Sent`/`\\Trash`/`\\Drafts`, else fallback."""
        marker = f"\\{attr}"
        for f in self.folders():
            if marker in f.flags:
                return f.name
        # Common conventions
        for f in self.folders():
            if f.display.lower() == fallback.lower():
                return f.name
        return fallback

    # ---- SMTP -----------------------------------------------------------

    def send(self, msg: EmailMessage) -> None:
        msg["From"] = msg.get("From") or self.email
        if not msg.get("Date"):
            msg["Date"] = email.utils.formatdate(localtime=True)
        if not msg.get("Message-ID"):
            msg["Message-ID"] = email.utils.make_msgid(domain=self.email.split("@", 1)[-1] or None)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(self.username, self.password)
            s.send_message(msg)
        sent_folder = self.find_special("Sent", "Sent")
        try:
            self.append(sent_folder, msg.as_bytes(), flags="(\\Seen)")
        except Exception:
            pass


def _extract_uid(meta: str) -> str | None:
    # meta looks like: "1234 (UID 567 FLAGS (\\Seen) BODY[HEADER.FIELDS ...] {123}"
    idx = meta.find("UID ")
    if idx == -1:
        return None
    rest = meta[idx + 4:]
    out: list[str] = []
    for ch in rest:
        if ch.isdigit():
            out.append(ch)
        else:
            break
    return "".join(out) or None


def _extract_flags(meta: str) -> str:
    idx = meta.find("FLAGS (")
    if idx == -1:
        return ""
    end = meta.find(")", idx)
    if end == -1:
        return ""
    return meta[idx + 7 : end]
