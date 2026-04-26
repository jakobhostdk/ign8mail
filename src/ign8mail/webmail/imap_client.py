"""IMAP helpers for the webmail interface."""

import email as _email
import email.header
import imaplib
import ssl
from dataclasses import dataclass, field


@dataclass
class MessageSummary:
    seq: str
    subject: str
    sender: str
    date: str
    seen: bool


@dataclass
class FullMessage:
    seq: str
    subject: str
    sender: str
    date: str
    seen: bool
    body_text: str = ""
    body_html: str = ""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return " ".join(out)


def _connect(host: str, username: str, password: str) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx)
    imap.login(username, password)
    return imap


def unread_count(host: str, username: str, password: str) -> int:
    """Return number of unseen messages, or -1 on connection error."""
    try:
        imap = _connect(host, username, password)
    except Exception:
        return -1
    try:
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "UNSEEN")
        return len(data[0].split()) if data[0] else 0
    except Exception:
        return -1
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def list_messages(host: str, username: str, password: str, limit: int = 100) -> list[MessageSummary]:
    """Return messages sorted newest first."""
    try:
        imap = _connect(host, username, password)
    except Exception:
        return []
    try:
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "ALL")
        ids = data[0].split()
        if not ids:
            return []
        recent = ids[-limit:][::-1]
        msgs = []
        for seq in recent:
            _, hdr_data = imap.fetch(seq, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if not hdr_data or not hdr_data[0]:
                continue
            raw_flags, raw_hdr = hdr_data[0][0], hdr_data[0][1]
            flags_str = raw_flags.decode() if isinstance(raw_flags, bytes) else str(raw_flags)
            seen = "\\Seen" in flags_str
            msg = _email.message_from_bytes(raw_hdr)
            msgs.append(MessageSummary(
                seq=seq.decode(),
                subject=_decode(msg.get("Subject")) or "(no subject)",
                sender=_decode(msg.get("From")) or "",
                date=msg.get("Date") or "",
                seen=seen,
            ))
        return msgs
    except Exception:
        return []
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_message(host: str, username: str, password: str, seq: str) -> FullMessage | None:
    try:
        imap = _connect(host, username, password)
    except Exception:
        return None
    try:
        imap.select("INBOX")
        _, data = imap.fetch(seq, "(FLAGS RFC822)")
        if not data or not data[0]:
            return None
        flags_str = data[0][0].decode() if isinstance(data[0][0], bytes) else ""
        seen = "\\Seen" in flags_str
        msg = _email.message_from_bytes(data[0][1])

        body_text = body_html = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if ct == "text/plain" and not body_text:
                    body_text = decoded
                elif ct == "text/html" and not body_html:
                    body_html = decoded
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    body_html = decoded
                else:
                    body_text = decoded

        imap.store(seq, "+FLAGS", "\\Seen")

        return FullMessage(
            seq=seq,
            subject=_decode(msg.get("Subject")) or "(no subject)",
            sender=_decode(msg.get("From")) or "",
            date=msg.get("Date") or "",
            seen=seen,
            body_text=body_text,
            body_html=body_html,
        )
    except Exception:
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass
