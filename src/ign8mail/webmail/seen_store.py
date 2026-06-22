"""Per-user read-state overlay for shared mailboxes.

Shared mailboxes are read by multiple users, but each user wants their own
unread count. We track that here, keyed by (user, mailbox, uid). Personal
mailboxes don't use this — they rely on Dovecot's \\Seen flag directly.
"""

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    user       TEXT NOT NULL,
    mailbox    TEXT NOT NULL,
    uid        INTEGER NOT NULL,
    seen_at    REAL NOT NULL,
    PRIMARY KEY (user, mailbox, uid)
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    return conn


def seen_uids(db_path: Path, user: str, mailbox: str, uids: list[int]) -> set[int]:
    """Return the subset of *uids* already marked seen for *user* in *mailbox*."""
    if not uids:
        return set()
    conn = _connect(db_path)
    try:
        placeholders = ",".join("?" * len(uids))
        rows = conn.execute(
            f"SELECT uid FROM seen WHERE user = ? AND mailbox = ? AND uid IN ({placeholders})",
            [user, mailbox, *uids],
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def is_seen(db_path: Path, user: str, mailbox: str, uid: int) -> bool:
    return uid in seen_uids(db_path, user, mailbox, [uid])


def mark_seen(db_path: Path, user: str, mailbox: str, uid: int) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen (user, mailbox, uid, seen_at) VALUES (?, ?, ?, ?)",
            (user, mailbox, uid, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def unread_count(db_path: Path, user: str, mailbox: str, all_uids: list[int]) -> int:
    """Count UIDs in *all_uids* that aren't yet seen for *user*."""
    if not all_uids:
        return 0
    return len(all_uids) - len(seen_uids(db_path, user, mailbox, all_uids))
