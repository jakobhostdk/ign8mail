"""Group a flat list of MessageSummary objects into conversation threads.

Strategy:
1. Union-find over Message-ID / In-Reply-To / References. Any two messages
   that share a Message-ID transitively become one thread.
2. Fallback: if a message has no threading headers, group with earlier
   messages in the same mailbox that share its normalized subject
   (Re:/Fwd: stripped). This keeps mailing-list-style replies together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .imap_client import MessageSummary


@dataclass
class Thread:
    root_mailbox: str
    root_uid: int
    subject: str
    latest_dt: datetime
    earliest_dt: datetime
    count: int
    unseen_count: int
    senders: list[str]
    mailboxes: list[str]
    messages: list[tuple[str, MessageSummary]] = field(default_factory=list)


_SUBJECT_PREFIX = re.compile(r"^\s*(re|fwd|fw|aw|sv)\s*:\s*", re.IGNORECASE)


def _normalize_subject(subject: str) -> str:
    s = subject or ""
    while True:
        stripped = _SUBJECT_PREFIX.sub("", s, count=1)
        if stripped == s:
            break
        s = stripped
    return s.strip().lower()


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, key: str) -> None:
        self.parent.setdefault(key, key)

    def find(self, key: str) -> str:
        self.add(key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def group_threads(entries: list[tuple[str, MessageSummary]]) -> list[Thread]:
    """entries: list of (mailbox, MessageSummary). Returns threads newest first."""
    if not entries:
        return []

    uf = _UnionFind()

    # Each message gets a synthetic node keyed by (mailbox, uid) so messages
    # without a Message-ID still participate.
    def node(mailbox: str, m: MessageSummary) -> str:
        return f"m:{mailbox}:{m.uid}"

    for mailbox, m in entries:
        uf.add(node(mailbox, m))
        if m.message_id:
            uf.union(node(mailbox, m), m.message_id)
        for ref in ([m.in_reply_to] if m.in_reply_to else []) + m.references:
            uf.union(node(mailbox, m), ref)

    # Subject fallback within the same mailbox: link messages whose normalized
    # subject matches and which aren't already in the same component via headers.
    subj_index: dict[tuple[str, str], str] = {}
    for mailbox, m in entries:
        key = (mailbox, _normalize_subject(m.subject))
        if not key[1]:
            continue
        n = node(mailbox, m)
        prev = subj_index.get(key)
        if prev is None:
            subj_index[key] = n
        else:
            uf.union(prev, n)

    groups: dict[str, list[tuple[str, MessageSummary]]] = {}
    for mailbox, m in entries:
        root = uf.find(node(mailbox, m))
        groups.setdefault(root, []).append((mailbox, m))

    threads: list[Thread] = []
    for msgs in groups.values():
        msgs.sort(key=lambda x: x[1].dt)
        earliest_mb, earliest_msg = msgs[0]
        latest_mb, latest_msg = msgs[-1]
        senders: list[str] = []
        seen_senders: set[str] = set()
        for _, m in msgs:
            if m.sender and m.sender not in seen_senders:
                seen_senders.add(m.sender)
                senders.append(m.sender)
        mailboxes: list[str] = []
        seen_mbs: set[str] = set()
        for mb, _ in msgs:
            if mb not in seen_mbs:
                seen_mbs.add(mb)
                mailboxes.append(mb)
        threads.append(Thread(
            root_mailbox=earliest_mb,
            root_uid=earliest_msg.uid,
            subject=latest_msg.subject or earliest_msg.subject or "(no subject)",
            latest_dt=latest_msg.dt,
            earliest_dt=earliest_msg.dt,
            count=len(msgs),
            unseen_count=sum(1 for _, m in msgs if not m.seen),
            senders=senders[:4],
            mailboxes=mailboxes,
            messages=msgs,
        ))

    threads.sort(key=lambda t: t.latest_dt, reverse=True)
    return threads
