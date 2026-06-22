"""IMAP helpers for the webmail interface.

Message identity is the IMAP UID (mailbox-scoped, stable across sessions).
Shared mailboxes track "seen for me" in the per-user overlay rather than
flipping the IMAP \\Seen flag.
"""

import email as _email
import email.header
import imaplib
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import seen_store


@dataclass
class MessageSummary:
    uid: int
    subject: str
    sender: str
    date: str
    seen: bool


@dataclass
class FullMessage:
    uid: int
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


_CACHE_TTL = 60
_local_cache: dict[str, tuple[int, float]] = {}
_redis_client: Any = None


def _redis() -> Any:
    global _redis_client
    if _redis_client is None:
        try:
            import redis
            r = redis.Redis(host="localhost", port=6379, decode_responses=True, socket_timeout=1)
            r.ping()
            _redis_client = r
        except Exception:
            _redis_client = False  # don't retry
    return _redis_client or None


def _cache_get(key: str) -> int | None:
    r = _redis()
    if r:
        try:
            v = r.get(key)
            return int(v) if v is not None else None
        except Exception:
            pass
    entry = _local_cache.get(key)
    if entry and time.monotonic() - entry[1] < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(key: str, value: int) -> None:
    r = _redis()
    if r:
        try:
            r.setex(key, _CACHE_TTL, value)
            return
        except Exception:
            pass
    _local_cache[key] = (value, time.monotonic())


def unread_count(
    host: str,
    mailbox: str,
    password: str,
    *,
    shared_for_user: str = "",
    overlay_db: Path | None = None,
) -> int:
    """Return number of unseen messages, or -1 on connection error.

    For shared mailboxes pass *shared_for_user* + *overlay_db*; the count is then
    computed against the per-user overlay rather than IMAP \\Seen.
    """
    is_shared = bool(shared_for_user and overlay_db)
    key = f"wm:unread:{host}:{mailbox}:{'shared:' + shared_for_user if is_shared else 'own'}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        imap = _connect(host, mailbox, password)
    except Exception:
        return -1
    try:
        imap.select("INBOX", readonly=True)
        if is_shared:
            _, data = imap.uid("SEARCH", "ALL")
            all_uids = [int(u) for u in (data[0].split() if data and data[0] else [])]
            count = seen_store.unread_count(overlay_db, shared_for_user, mailbox, all_uids)  # type: ignore[arg-type]
        else:
            _, data = imap.search(None, "UNSEEN")
            count = len(data[0].split()) if data[0] else 0
        _cache_set(key, count)
        return count
    except Exception:
        return -1
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def list_messages(
    host: str,
    mailbox: str,
    password: str,
    limit: int = 100,
    *,
    shared_for_user: str = "",
    overlay_db: Path | None = None,
) -> list[MessageSummary]:
    """Return messages sorted newest first.

    For shared mailboxes the `seen` flag reflects the per-user overlay; the
    IMAP \\Seen flag is ignored.
    """
    is_shared = bool(shared_for_user and overlay_db)
    try:
        imap = _connect(host, mailbox, password)
    except Exception:
        return []
    try:
        imap.select("INBOX", readonly=True)
        _, data = imap.uid("SEARCH", "ALL")
        uids = data[0].split() if data and data[0] else []
        if not uids:
            return []
        recent = uids[-limit:][::-1]
        seen_overlay: set[int] = set()
        if is_shared:
            int_uids = [int(u) for u in recent]
            seen_overlay = seen_store.seen_uids(
                overlay_db,  # type: ignore[arg-type]
                shared_for_user, mailbox, int_uids,
            )
        msgs = []
        for uid in recent:
            _, hdr_data = imap.uid(
                "FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            if not hdr_data or not hdr_data[0]:
                continue
            raw_flags, raw_hdr = hdr_data[0][0], hdr_data[0][1]
            flags_str = raw_flags.decode() if isinstance(raw_flags, bytes) else str(raw_flags)
            uid_int = int(uid)
            if is_shared:
                seen = uid_int in seen_overlay
            else:
                seen = "\\Seen" in flags_str
            msg = _email.message_from_bytes(raw_hdr)
            msgs.append(MessageSummary(
                uid=uid_int,
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


def fetch_message(
    host: str,
    mailbox: str,
    password: str,
    uid: int,
    *,
    shared_for_user: str = "",
    overlay_db: Path | None = None,
) -> FullMessage | None:
    """Fetch one message by UID. Marks it seen (overlay for shared, IMAP \\Seen for personal)."""
    is_shared = bool(shared_for_user and overlay_db)
    try:
        imap = _connect(host, mailbox, password)
    except Exception:
        return None
    try:
        imap.select("INBOX")
        _, data = imap.uid("FETCH", str(uid), "(FLAGS RFC822)")
        if not data or not data[0]:
            return None
        flags_str = data[0][0].decode() if isinstance(data[0][0], bytes) else ""
        seen_flag = "\\Seen" in flags_str
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

        if is_shared:
            seen_store.mark_seen(overlay_db, shared_for_user, mailbox, uid)  # type: ignore[arg-type]
            displayed_seen = True
        else:
            imap.uid("STORE", str(uid), "+FLAGS", "\\Seen")
            displayed_seen = seen_flag or True

        return FullMessage(
            uid=uid,
            subject=_decode(msg.get("Subject")) or "(no subject)",
            sender=_decode(msg.get("From")) or "",
            date=msg.get("Date") or "",
            seen=displayed_seen,
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


def fetch_recent_summaries(
    host: str,
    mailbox: str,
    password: str,
    *,
    limit: int = 50,
    shared_for_user: str = "",
    overlay_db: Path | None = None,
) -> list[MessageSummary]:
    """Thin wrapper used by the unified inbox; returns newest *limit* messages."""
    return list_messages(
        host, mailbox, password, limit=limit,
        shared_for_user=shared_for_user, overlay_db=overlay_db,
    )
