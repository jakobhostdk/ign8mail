"""TUI that shows new (unseen) mail across every mailbox in the vault.

Keys:
    j/k  ↓/↑    Navigate
    Enter       Mark message read (and leave it in INBOX)
    d           Move to Trash
    o           Open a link from the body
    y           Copy body to clipboard (OSC 52)
    g           Refresh
    q           Quit
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape as _esc
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from ign8mail.tui.app import (
    LinksScreen,
    _extract_links,
    _html_to_text,
    _just_name,
    _short_date,
    _truncate,
    load_accounts,
)
from ign8mail.tui.client import FullMessage, MailClient, MessageSummary


@dataclass
class NewItem:
    username: str
    summary: MessageSummary


def gather_new(
    accounts: dict[str, str],
    imap_host: str,
    domain: str,
    smtp_host: str,
    workers: int = 8,
) -> tuple[dict[str, MailClient], list[NewItem], dict[str, str]]:
    """Open one IMAP client per account in parallel and collect INBOX UNSEEN.

    Returns (clients, items_sorted_newest_first, errors).
    Clients stay open so the TUI can re-use them for fetch/move/store calls.
    """
    clients: dict[str, MailClient] = {}
    items: list[NewItem] = []
    errors: dict[str, str] = {}

    def _probe(
        username: str, password: str
    ) -> tuple[str, MailClient | None, list[MessageSummary], str]:
        client = MailClient(
            host=imap_host,
            username=username,
            password=password,
            email_addr=f"{username}@{domain}",
            smtp_host=smtp_host,
        )
        try:
            client.connect()
        except Exception as exc:
            return username, None, [], f"login: {exc}"
        try:
            msgs = client.list_messages("INBOX", limit=200, unseen_only=True)
        except Exception as exc:
            return username, client, [], f"list: {exc}"
        return username, client, msgs, ""

    if not accounts:
        return clients, items, errors

    with ThreadPoolExecutor(max_workers=min(len(accounts), workers)) as pool:
        futs = {pool.submit(_probe, u, p): u for u, p in accounts.items()}
        for fut in as_completed(futs):
            username, client, msgs, err = fut.result()
            if client is not None:
                clients[username] = client
            if err:
                errors[username] = err
                continue
            for s in msgs:
                items.append(NewItem(username=username, summary=s))

    items.sort(key=lambda i: _sort_key(i.summary.date), reverse=True)
    return clients, items, errors


def _sort_key(date_str: str) -> float:
    import email.utils as eu

    try:
        dt = eu.parsedate_to_datetime(date_str)
        return dt.timestamp()
    except Exception:
        return 0.0


class NewMailApp(App[None]):
    CSS = """
    #list-pane { width: 2fr; border-right: solid $primary 50%; }
    #body-pane { width: 3fr; padding: 1 2; overflow-y: auto; }
    .pane-title { background: $boost; padding: 0 1; color: $text; }
    ListView { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("g", "refresh", "Refresh"),
        Binding("d", "delete", "Trash"),
        Binding("enter", "mark_read", "Mark read"),
        Binding("o", "open_link", "Open link"),
        Binding("y", "copy_body", "Copy body"),
        Binding("j", "down", show=False),
        Binding("k", "up", show=False),
    ]

    def __init__(
        self,
        vault_path: Path,
        domain: str,
        imap_host: str,
        smtp_host: str,
    ) -> None:
        super().__init__()
        self.vault_path = vault_path
        self.domain = domain
        self.imap_host = imap_host
        self.smtp_host = smtp_host
        self.clients: dict[str, MailClient] = {}
        self.items: list[NewItem] = []
        self.current_body: FullMessage | None = None

    # ---- layout -----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="list-pane"):
                yield Static("New mail", classes="pane-title", id="list-title")
                yield ListView(id="new-list")
            with Vertical(id="body-pane"):
                yield Static("Body", classes="pane-title")
                yield Static("Loading…", id="body-view")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"ign8mail — new mail ({self.domain or 'all accounts'})"
        self.action_refresh()

    # ---- refresh ----------------------------------------------------

    def action_refresh(self) -> None:
        self.query_one("#list-title", Static).update("New mail — scanning…")
        self.query_one("#body-view", Static).update("")
        self._refresh_worker()

    @work(thread=True, exclusive=True)
    def _refresh_worker(self) -> None:
        # Close stale clients before re-opening — keeps things simple.
        for c in self.clients.values():
            try:
                c.close()
            except Exception:
                pass
        self.clients = {}

        accounts = load_accounts(self.vault_path)
        clients, items, errors = gather_new(
            accounts, self.imap_host, self.domain, self.smtp_host
        )
        self.call_from_thread(self._populate, clients, items, errors)

    def _populate(
        self,
        clients: dict[str, MailClient],
        items: list[NewItem],
        errors: dict[str, str],
    ) -> None:
        self.clients = clients
        self.items = items
        view = self.query_one("#new-list", ListView)
        view.clear()
        for it in items:
            sender = _truncate(_just_name(it.summary.sender), 22)
            subj = _truncate(it.summary.subject, 45)
            date = _short_date(it.summary.date)
            acct = _truncate(it.username, 14)
            label = f"● {acct:14} {sender:22} {subj:45} {date}"
            view.append(ListItem(Label(label)))

        total = len(items)
        accts_with_mail = len({i.username for i in items})
        title = f"New mail ({total} across {accts_with_mail} of {len(clients)} accts)"
        if errors:
            title += f"  [red]· {len(errors)} err[/red]"
        self.query_one("#list-title", Static).update(title)

        if items:
            view.focus()
            view.index = 0
        else:
            err_lines = "\n".join(f"[red]{u}:[/red] {e}" for u, e in errors.items())
            msg = "[bold green]Inbox zero across all accounts.[/bold green]"
            if err_lines:
                msg += "\n\n" + err_lines
            self.query_one("#body-view", Static).update(msg)
            self.current_body = None

    # ---- selection / body load --------------------------------------

    @on(ListView.Highlighted, "#new-list")
    def on_highlighted(self, event: ListView.Highlighted) -> None:
        self._load_body_for(event.list_view.index)

    @on(ListView.Selected, "#new-list")
    def on_selected(self, event: ListView.Selected) -> None:
        self._load_body_for(event.list_view.index)

    def _load_body_for(self, idx: int | None) -> None:
        if idx is None or idx < 0 or idx >= len(self.items):
            return
        item = self.items[idx]
        if self.current_body and self.current_body.uid == item.summary.uid:
            return
        self._fetch_body(item.username, item.summary.uid)

    @work(thread=True, exclusive=True)
    def _fetch_body(self, username: str, uid: str) -> None:
        client = self.clients.get(username)
        if client is None:
            return
        try:
            msg = client.fetch("INBOX", uid)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Fetch failed: {exc}", severity="error")
            return
        self.call_from_thread(self._render_body, msg)

    def _render_body(self, msg: FullMessage | None) -> None:
        self.current_body = msg
        if msg is None:
            self.query_one("#body-view", Static).update("[red]Could not load message.[/red]")
            return
        body = msg.body_text or _html_to_text(msg.body_html) or "(empty)"
        body = _esc(body.replace("\r\n", "\n").replace("\r", "\n"))
        cc_line = f"[bold]Cc:[/bold] {_esc(msg.cc)}\n" if msg.cc else ""
        header = (
            f"[bold]From:[/bold] {_esc(msg.sender)}\n"
            f"[bold]To:[/bold] {_esc(msg.to)}\n"
            + cc_line
            + f"[bold]Date:[/bold] {_esc(msg.date)}\n"
            f"[bold]Subject:[/bold] {_esc(msg.subject)}\n"
            + "─" * 60 + "\n"
        )
        self.query_one("#body-view", Static).update(header + body)

    # ---- actions ----------------------------------------------------

    def action_mark_read(self) -> None:
        idx, item = self._current()
        if item is None:
            return
        client = self.clients.get(item.username)
        if client is None:
            return

        def _do() -> None:
            try:
                client.mark("INBOX", item.summary.uid, "\\Seen", add=True)
            except Exception as exc:
                self.call_from_thread(self.notify, f"Mark seen failed: {exc}", severity="error")
                return
            self.call_from_thread(self._drop_index, idx)

        self.run_worker(_do, thread=True, exclusive=False)

    def action_delete(self) -> None:
        idx, item = self._current()
        if item is None:
            return
        client = self.clients.get(item.username)
        if client is None:
            return

        def _do() -> None:
            trash = "Trash"
            try:
                trash = client.find_special("Trash", "Trash")
            except Exception:
                pass
            try:
                ok = client.move("INBOX", item.summary.uid, trash)
            except Exception as exc:
                self.call_from_thread(self.notify, f"Trash failed: {exc}", severity="error")
                return
            if not ok:
                self.call_from_thread(self.notify, "Move to Trash failed", severity="error")
                return
            self.call_from_thread(self._drop_index, idx)

        self.run_worker(_do, thread=True, exclusive=False)

    def action_open_link(self) -> None:
        import webbrowser

        msg = self.current_body
        if msg is None:
            return
        links = _extract_links(msg)
        if not links:
            self.notify("No links in this message.")
            return
        if len(links) == 1:
            webbrowser.open(links[0])
            self.notify(f"Opened {links[0][:60]}")
            return

        def _picked(url: str | None) -> None:
            if url:
                webbrowser.open(url)
                self.notify(f"Opened {url[:60]}")

        self.push_screen(LinksScreen(links), _picked)

    def action_copy_body(self) -> None:
        msg = self.current_body
        if msg is None:
            return
        body = msg.body_text or _html_to_text(msg.body_html)
        if not body:
            self.notify("Nothing to copy.")
            return
        self.copy_to_clipboard(body)
        self.notify("Body copied to clipboard.")

    def action_down(self) -> None:
        view = self.query_one("#new-list", ListView)
        view.action_cursor_down()

    def action_up(self) -> None:
        view = self.query_one("#new-list", ListView)
        view.action_cursor_up()

    # ---- helpers ----------------------------------------------------

    def _current(self) -> tuple[int, NewItem | None]:
        view = self.query_one("#new-list", ListView)
        idx = view.index
        if idx is None or idx < 0 or idx >= len(self.items):
            return -1, None
        return idx, self.items[idx]

    def _drop_index(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.items):
            return
        self.items.pop(idx)
        view = self.query_one("#new-list", ListView)
        try:
            view.pop(idx)
        except Exception:
            pass
        total = len(self.items)
        accts_with_mail = len({i.username for i in self.items})
        self.query_one("#list-title", Static).update(
            f"New mail ({total} across {accts_with_mail} of {len(self.clients)} accts)"
        )
        if not self.items:
            self.current_body = None
            self.query_one("#body-view", Static).update(
                "[bold green]Inbox zero across all accounts.[/bold green]"
            )
            return
        new_idx = min(idx, total - 1)
        view.index = new_idx
        self._load_body_for(new_idx)

    def on_unmount(self) -> None:
        for c in self.clients.values():
            try:
                c.close()
            except Exception:
                pass
