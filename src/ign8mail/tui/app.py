"""Textual TUI mail reader — multi-account.

Layout:
    +-------------------------------------------------------------+
    | Header                                                      |
    +-----------------+------------------+------------------------+
    | Accounts/Tree   | Messages         | Body                   |
    | ▼ alice         | ● from / subject |                        |
    |   INBOX (3)     | ...              |                        |
    |   Sent          |                  |                        |
    | ▶ somenew       |                  |                        |
    +-----------------+------------------+------------------------+
    | Footer                                                      |
    +-------------------------------------------------------------+

Accounts come from the local vault (`mailbox_passwords`). Autocreated
mailboxes with a stored password appear automatically. Archived mailboxes
are hidden via `.ign8mail/tui_archived.json` — toggle visibility with `A`.

Keys:
    Enter / Space   Expand/open a node
    j/k  ↓/↑        Navigate
    Tab             Cycle pane focus
    r  R  f         Reply / Reply-all / Forward
    c               Compose new (uses current account)
    d               Delete (move to Trash)
    u               Toggle Unread
    o               Open a link from the current message
    y               Copy message body to clipboard (OSC 52)
    m               Toggle mouse — turn off to drag-select with the terminal natively
    a               Archive (hide) current account
    A               Toggle show archived
    g               Refresh accounts + folders
    ?               Help
    q               Quit
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import webbrowser
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static, Tree
from textual.widgets.tree import TreeNode

from ign8mail.tui.client import FullMessage, MailClient, MessageSummary


# --------------------------- compose screen ----------------------------


class ComposeScreen(ModalScreen[EmailMessage | None]):
    """Modal screen for composing/replying. Body is edited in $EDITOR."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "send", "Send"),
        Binding("ctrl+e", "edit_body", "Edit body in $EDITOR"),
    ]

    CSS = """
    ComposeScreen { align: center middle; }
    #compose-box {
        width: 90%; height: 90%;
        border: round $primary; background: $surface;
        padding: 1 2;
    }
    #compose-box Input { margin-bottom: 1; }
    #compose-status { color: $text-muted; }
    #compose-body { height: 1fr; border: round $secondary; padding: 1; overflow-y: auto; }
    """

    def __init__(
        self,
        from_addr: str,
        to: str = "",
        cc: str = "",
        subject: str = "",
        body: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> None:
        super().__init__()
        self._from = from_addr
        self._to = to
        self._cc = cc
        self._subject = subject
        self._body = body
        self._in_reply_to = in_reply_to
        self._references = references

    def compose(self) -> ComposeResult:
        with Vertical(id="compose-box"):
            yield Label(f"Compose — from {self._from}")
            yield Input(value=self._to, placeholder="To:", id="to")
            yield Input(value=self._cc, placeholder="Cc:", id="cc")
            yield Input(value=self._subject, placeholder="Subject:", id="subject")
            yield Static(self._body or "(empty — Ctrl+E to edit body in $EDITOR)", id="compose-body")
            yield Label("Ctrl+E edit body  ·  Ctrl+S send  ·  Esc cancel", id="compose-status")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_send(self) -> None:
        to = self.query_one("#to", Input).value.strip()
        cc = self.query_one("#cc", Input).value.strip()
        subject = self.query_one("#subject", Input).value.strip()
        if not to:
            self.notify("Recipient required", severity="warning")
            return
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        if self._in_reply_to:
            msg["In-Reply-To"] = self._in_reply_to
        if self._references:
            msg["References"] = self._references
        msg.set_content(self._body or "")
        self.dismiss(msg)

    def action_edit_body(self) -> None:
        editor = os.environ.get("EDITOR", "vi")
        with self.app.suspend():
            with tempfile.NamedTemporaryFile(mode="w+", suffix=".eml", delete=False) as tf:
                tf.write(self._body)
                path = tf.name
            try:
                subprocess.run([editor, path], check=False)
                with open(path, encoding="utf-8") as f:
                    self._body = f.read()
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        preview = self._body if self._body.strip() else "(empty — Ctrl+E to edit body in $EDITOR)"
        self.query_one("#compose-body", Static).update(preview)


# --------------------------- links screen -------------------------------


class LinksScreen(ModalScreen[str | None]):
    """Modal list of URLs found in the current message."""

    BINDINGS = [
        Binding("escape,q", "cancel", "Cancel"),
        Binding("enter", "open_selected", "Open", show=False),
        Binding("y", "copy_selected", "Copy URL"),
    ]

    CSS = """
    LinksScreen { align: center middle; }
    #links-box {
        width: 80%; height: 80%;
        border: round $primary; background: $surface;
        padding: 1 2;
    }
    #links-status { color: $text-muted; }
    """

    def __init__(self, links: list[str]) -> None:
        super().__init__()
        self._links = links

    def compose(self) -> ComposeResult:
        with Vertical(id="links-box"):
            yield Label(f"Links ({len(self._links)})")
            yield ListView(id="links-list")
            yield Label("Enter open · y copy · Esc cancel", id="links-status")

    def on_mount(self) -> None:
        view = self.query_one("#links-list", ListView)
        for i, link in enumerate(self._links, 1):
            view.append(ListItem(Label(f"{i:>2}. {link}")))
        if self._links:
            view.index = 0
        view.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_open_selected(self) -> None:
        view = self.query_one("#links-list", ListView)
        idx = view.index
        if idx is None or idx < 0 or idx >= len(self._links):
            self.dismiss(None)
            return
        self.dismiss(self._links[idx])

    def action_copy_selected(self) -> None:
        view = self.query_one("#links-list", ListView)
        idx = view.index
        if idx is None or idx < 0 or idx >= len(self._links):
            return
        url = self._links[idx]
        self.app.copy_to_clipboard(url)
        self.app.notify(f"Copied {url[:60]}{'…' if len(url) > 60 else ''}")

    @on(ListView.Selected, "#links-list")
    def on_link_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None or idx < 0 or idx >= len(self._links):
            self.dismiss(None)
            return
        self.dismiss(self._links[idx])


# --------------------------- help screen --------------------------------


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,?", "app.pop_screen", "Close")]

    CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 60; height: auto; border: round $primary;
        background: $surface; padding: 1 2;
    }
    """

    HELP_TEXT = (
        "[bold]Navigation[/bold]\n"
        "  j/↓ next   k/↑ prev   Enter/Space expand-open   Tab cycle pane\n\n"
        "[bold]Mail[/bold]\n"
        "  c compose   r reply   R reply-all   f forward\n"
        "  d delete    u toggle unread   o open link   y copy body\n\n"
        "[bold]Clipboard[/bold]\n"
        "  y copies the whole body via OSC 52.\n"
        "  m toggles mouse capture — turn off, then drag-select and\n"
        "  copy with your terminal as usual (Cmd+C / Ctrl+Shift+C).\n"
        "  Shift (Linux) / Option (macOS) + drag also bypasses capture\n"
        "  without toggling.\n\n"
        "[bold]Accounts[/bold]\n"
        "  a archive (hide) current account\n"
        "  A toggle show-archived\n"
        "  g refresh\n\n"
        "[bold]Other[/bold]\n"
        "  ? help    q quit"
    )

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self.HELP_TEXT)


# --------------------------- account discovery -------------------------


def load_accounts(vault_path: Path) -> dict[str, str]:
    """Return {username: password} for every openable mailbox in the vault."""
    if not vault_path.exists():
        return {}
    from ign8mail import vault as _vault
    data = _vault.load(vault_path)
    return dict(data.get("mailbox_passwords") or {})


def load_archived(archived_path: Path) -> set[str]:
    if not archived_path.exists():
        return set()
    try:
        return set(json.loads(archived_path.read_text()))
    except Exception:
        return set()


def save_archived(archived_path: Path, archived: set[str]) -> None:
    archived_path.parent.mkdir(parents=True, exist_ok=True)
    archived_path.write_text(json.dumps(sorted(archived), indent=2))


# --------------------------- main app -----------------------------------


class MailApp(App[None]):
    CSS = """
    #left { width: 30; border-right: solid $primary 50%; }
    #messages { width: 2fr; border-right: solid $primary 50%; }
    #body { width: 3fr; padding: 1 2; overflow-y: auto; }
    .pane-title { background: $boost; padding: 0 1; color: $text; }
    Tree { height: 1fr; }
    ListView { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("c", "compose", "Compose"),
        Binding("r", "reply", "Reply"),
        Binding("R", "reply_all", "Reply-all"),
        Binding("f", "forward", "Forward"),
        Binding("d", "delete", "Delete"),
        Binding("u", "toggle_unread", "Unread"),
        Binding("o", "open_link", "Open link"),
        Binding("y", "copy_body", "Copy body"),
        Binding("m", "toggle_mouse", "Mouse"),
        Binding("a", "archive_account", "Archive acct"),
        Binding("A", "toggle_show_archived", "Show archived"),
        Binding("g", "refresh", "Refresh"),
        Binding("tab", "focus_next", "Tab"),
        Binding("j", "down", show=False),
        Binding("k", "up", show=False),
    ]

    REFRESH_INTERVAL = 30.0  # seconds — re-read vault to pick up new mailboxes

    def __init__(
        self,
        vault_path: Path,
        archived_path: Path,
        domain: str,
        imap_host: str,
        smtp_host: str,
        focus_account: str | None = None,
    ) -> None:
        super().__init__()
        self.vault_path = vault_path
        self.archived_path = archived_path
        self.domain = domain
        self.imap_host = imap_host
        self.smtp_host = smtp_host
        self.focus_account = focus_account
        self.archived: set[str] = load_archived(archived_path)
        self.show_archived: bool = False
        self.clients: dict[str, MailClient] = {}
        self.messages: list[MessageSummary] = []
        self.current_account: str | None = None
        self.current_folder: str | None = None
        self.current_message: FullMessage | None = None
        self._account_nodes: dict[str, TreeNode] = {}
        self._mouse_on: bool = True

    # ---- layout -----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                yield Static("Accounts", classes="pane-title", id="left-title")
                tree: Tree[dict] = Tree("Mailboxes", id="accounts")
                tree.show_root = False
                yield tree
            with Vertical(id="messages"):
                yield Static("Messages", classes="pane-title", id="messages-title")
                yield ListView(id="message-list")
            with Vertical(id="body"):
                yield Static("Body", classes="pane-title")
                yield Static("Select a folder to begin.", id="body-view")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"ign8mail — {self.domain or 'mail reader'}"
        self.refresh_accounts()
        self.set_interval(self.REFRESH_INTERVAL, self.refresh_accounts)

    # ---- account tree -----------------------------------------------

    def refresh_accounts(self) -> None:
        accounts = load_accounts(self.vault_path)
        tree = self.query_one("#accounts", Tree)
        root = tree.root
        existing = self._account_nodes
        # remove nodes for vanished accounts
        for username in list(existing.keys()):
            if username not in accounts:
                existing[username].remove()
                del existing[username]
        # filter visible
        visible = [u for u in sorted(accounts) if self.show_archived or u not in self.archived]
        for username in visible:
            if username not in existing:
                label = self._account_label(username, accounts[username])
                node = root.add(label, data={"kind": "account", "username": username}, expand=False)
                # placeholder so the expand arrow shows
                node.add_leaf("…loading", data={"kind": "placeholder"})
                existing[username] = node
            else:
                existing[username].set_label(self._account_label(username, accounts[username]))
        # remove nodes that are now hidden (archived without show_archived)
        for username in list(existing.keys()):
            if username not in visible:
                existing[username].remove()
                del existing[username]
        title = f"Accounts ({len(visible)}/{len(accounts)}" + (" +arch" if self.show_archived else "") + ")"
        self.query_one("#left-title", Static).update(title)
        # focus the requested account once
        if self.focus_account and self.focus_account in existing:
            tree.select_node(existing[self.focus_account])
            existing[self.focus_account].expand()
            self.focus_account = None

    def _account_label(self, username: str, _password: str) -> str:
        marker = " [dim](archived)[/dim]" if username in self.archived else ""
        return f"[bold]{username}[/bold]@{self.domain}{marker}"

    def _client_for(self, username: str) -> MailClient | None:
        if username in self.clients:
            return self.clients[username]
        accounts = load_accounts(self.vault_path)
        pw = accounts.get(username)
        if not pw:
            self.notify(f"No password for {username}", severity="error")
            return None
        client = MailClient(
            host=self.imap_host,
            username=username,
            password=pw,
            email_addr=f"{username}@{self.domain}",
            smtp_host=self.smtp_host,
        )
        try:
            client.connect()
        except Exception as e:
            self.notify(f"IMAP login failed for {username}: {e}", severity="error")
            return None
        self.clients[username] = client
        return client

    # ---- tree interaction -------------------------------------------

    @on(Tree.NodeExpanded, "#accounts")
    def on_account_expand(self, event: Tree.NodeExpanded) -> None:
        data = event.node.data
        if not data or data.get("kind") != "account":
            return
        # only load on first expansion (when placeholder is present)
        if not event.node.children or (event.node.children[0].data or {}).get("kind") != "placeholder":
            return
        self.load_folders(event.node, data["username"])

    @on(Tree.NodeSelected, "#accounts")
    def on_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data or {}
        kind = data.get("kind")
        if kind == "account":
            event.node.expand()
        elif kind == "folder":
            self.current_account = data["username"]
            self.current_folder = data["folder"]
            self.load_messages(data["username"], data["folder"])

    @work(thread=True)
    def load_folders(self, node: TreeNode, username: str) -> None:
        client = self._client_for(username)
        if client is None:
            self.call_from_thread(node.remove_children)
            return
        try:
            folders = client.folders()
        except Exception as e:
            self.call_from_thread(self.notify, f"List folders failed: {e}", severity="error")
            folders = []
        self.call_from_thread(self._populate_folders, node, username, folders)

    def _populate_folders(self, node: TreeNode, username: str, folders) -> None:
        node.remove_children()
        for f in folders:
            node.add_leaf(
                f.display,
                data={"kind": "folder", "username": username, "folder": f.name},
            )

    # ---- message loading --------------------------------------------

    @work(thread=True, exclusive=True)
    def load_messages(self, username: str, folder: str) -> None:
        client = self._client_for(username)
        if client is None:
            return
        try:
            msgs = client.list_messages(folder)
        except Exception as e:
            self.call_from_thread(self.notify, f"List messages failed: {e}", severity="error")
            return
        self.call_from_thread(self._populate_messages, msgs, username, folder)

    def _populate_messages(self, msgs: list[MessageSummary], username: str, folder: str) -> None:
        # Skip if user already navigated away from this folder before the worker finished
        if self.current_account != username or self.current_folder != folder:
            return
        self.messages = msgs
        view = self.query_one("#message-list", ListView)
        # `clear()` is async — items aren't gone from the NodeList yet when we append below.
        # We don't set per-item IDs (IMAP UIDs can collide across folders, which raises
        # DuplicateIds during the brief window before clear() completes); the selected
        # index is enough to recover the UID from self.messages.
        view.clear()
        for m in msgs:
            marker = " " if m.seen else "●"
            sender = _truncate(_just_name(m.sender), 22)
            subj = _truncate(m.subject, 50)
            date = _short_date(m.date)
            label = f"{marker} {sender:22} {subj:50} {date}"
            view.append(ListItem(Label(label)))
        self.current_message = None
        self.query_one("#messages-title", Static).update(f"{username} / {folder} ({len(msgs)})")
        if msgs:
            # Focus the list so j/k navigates messages (not the folder tree).
            # Highlight the first message and load its body so the right pane
            # isn't blank — the Highlighted handler takes over for subsequent
            # navigation.
            view.focus()
            view.index = 0
        else:
            self.query_one("#body-view", Static).update("")

    def _preview_highlighted(self, idx: int | None) -> None:
        if not self.current_account or not self.current_folder:
            return
        if idx is None or idx < 0 or idx >= len(self.messages):
            return
        uid = self.messages[idx].uid
        # Skip re-fetching the message we're already showing (e.g. when the
        # Highlighted event fires from a programmatic index change right after
        # an explicit load).
        if self.current_message and self.current_message.uid == uid:
            return
        self.load_body(self.current_account, self.current_folder, uid)

    @on(ListView.Highlighted, "#message-list")
    def on_message_highlighted(self, event: ListView.Highlighted) -> None:
        self._preview_highlighted(event.list_view.index)

    @on(ListView.Selected, "#message-list")
    def on_message_selected(self, event: ListView.Selected) -> None:
        self._preview_highlighted(event.list_view.index)

    @work(thread=True, exclusive=True)
    def load_body(self, username: str, folder: str, uid: str) -> None:
        client = self._client_for(username)
        if client is None:
            return
        try:
            msg = client.fetch(folder, uid)
        except Exception as e:
            self.call_from_thread(self.notify, f"Fetch failed: {e}", severity="error")
            return
        if msg:
            try:
                client.mark(folder, uid, "\\Seen", add=True)
            except Exception:
                pass
        self.call_from_thread(self._show_body, msg)

    def _show_body(self, msg: FullMessage | None) -> None:
        from rich.markup import escape as _esc

        self.current_message = msg
        if msg is None:
            self.query_one("#body-view", Static).update("[red]Could not load message.[/red]")
            return
        body = msg.body_text or _html_to_text(msg.body_html) or "(empty)"
        # Normalize CRLF/CR → LF (a bare \r returns the terminal cursor and
        # visually erases the line) and escape brackets so mail content isn't
        # interpreted as Rich markup. The static [bold] tags below stay literal.
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

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh(self) -> None:
        self.refresh_accounts()
        if self.current_account and self.current_folder:
            self.load_messages(self.current_account, self.current_folder)

    def action_down(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_down()
        elif isinstance(focused, Tree):
            focused.action_cursor_down()

    def action_up(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_up()
        elif isinstance(focused, Tree):
            focused.action_cursor_up()

    def action_compose(self) -> None:
        from_addr = self._from_addr()
        if not from_addr:
            self.notify("Select an account first", severity="warning")
            return
        self.push_screen(ComposeScreen(from_addr=from_addr), self._send_callback)

    def action_reply(self) -> None:
        msg = self.current_message
        if not msg or not self.current_account:
            return
        from_addr = self._from_addr()
        subj = msg.subject if msg.subject.lower().startswith("re:") else f"Re: {msg.subject}"
        refs = (msg.references + " " + msg.message_id).strip()
        self.push_screen(
            ComposeScreen(
                from_addr=from_addr,
                to=msg.sender,
                subject=subj,
                body=_quote_body(msg),
                in_reply_to=msg.message_id,
                references=refs,
            ),
            self._send_callback,
        )

    def action_reply_all(self) -> None:
        msg = self.current_message
        if not msg or not self.current_account:
            return
        from_addr = self._from_addr()
        me = from_addr.lower()
        recipients = [a for _, a in getaddresses([msg.sender, msg.to]) if a and a.lower() != me]
        cc_list = [a for _, a in getaddresses([msg.cc]) if a and a.lower() != me]
        to = ", ".join(dict.fromkeys(recipients))
        cc = ", ".join(dict.fromkeys(cc_list))
        subj = msg.subject if msg.subject.lower().startswith("re:") else f"Re: {msg.subject}"
        refs = (msg.references + " " + msg.message_id).strip()
        self.push_screen(
            ComposeScreen(
                from_addr=from_addr,
                to=to, cc=cc, subject=subj,
                body=_quote_body(msg),
                in_reply_to=msg.message_id, references=refs,
            ),
            self._send_callback,
        )

    def action_forward(self) -> None:
        msg = self.current_message
        if not msg or not self.current_account:
            return
        from_addr = self._from_addr()
        subj = msg.subject if msg.subject.lower().startswith("fwd:") else f"Fwd: {msg.subject}"
        body = (
            "\n\n--- Forwarded message ---\n"
            f"From: {msg.sender}\nDate: {msg.date}\nSubject: {msg.subject}\nTo: {msg.to}\n\n"
            f"{msg.body_text or _html_to_text(msg.body_html)}"
        )
        self.push_screen(ComposeScreen(from_addr=from_addr, subject=subj, body=body), self._send_callback)

    def action_delete(self) -> None:
        msg = self.current_message
        if not msg or not self.current_account or not self.current_folder:
            return
        client = self._client_for(self.current_account)
        if not client:
            return
        trash = client.find_special("Trash", "Trash")
        try:
            if trash.lower() == self.current_folder.lower():
                client.mark(self.current_folder, msg.uid, "\\Deleted", add=True)
                client._conn().expunge()
            else:
                if not client.move(self.current_folder, msg.uid, trash):
                    self.notify("Move to Trash failed", severity="error")
                    return
        except Exception as e:
            self.notify(f"Delete failed: {e}", severity="error")
            return
        self.notify("Deleted.")
        self.load_messages(self.current_account, self.current_folder)

    def action_open_link(self) -> None:
        msg = self.current_message
        if not msg:
            self.notify("No message selected", severity="warning")
            return
        links = _extract_links(msg)
        if not links:
            self.notify("No links found in message", severity="warning")
            return
        self.push_screen(LinksScreen(links), self._open_link_callback)

    def action_toggle_mouse(self) -> None:
        """Toggle Textual mouse capture so the terminal can do native select/copy."""
        drv = getattr(self, "_driver", None)
        if drv is None:
            self.notify("Driver not ready", severity="warning")
            return
        if self._mouse_on:
            disable = getattr(drv, "_disable_mouse_support", None)
            if disable is None:
                self.notify("Driver doesn't support mouse toggle", severity="warning")
                return
            disable()
            self._mouse_on = False
            self.notify("Mouse off — drag to select, terminal copies natively. m to restore.")
        else:
            enable = getattr(drv, "_enable_mouse_support", None)
            if enable is None:
                self.notify("Driver doesn't support mouse toggle", severity="warning")
                return
            enable()
            self._mouse_on = True
            self.notify("Mouse on")

    def action_copy_body(self) -> None:
        msg = self.current_message
        if not msg:
            self.notify("No message selected", severity="warning")
            return
        text = msg.body_text or _html_to_text(msg.body_html) or ""
        if not text.strip():
            self.notify("Empty body", severity="warning")
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.copy_to_clipboard(text)
        self.notify(f"Copied body ({len(text)} chars)")

    def _open_link_callback(self, url: str | None) -> None:
        if not url:
            return
        try:
            webbrowser.open(url)
        except Exception as e:
            self.notify(f"Open failed: {e}", severity="error")
            return
        self.notify(f"Opened {url[:60]}{'…' if len(url) > 60 else ''}")

    def action_toggle_unread(self) -> None:
        msg = self.current_message
        if not msg or not self.current_account or not self.current_folder:
            return
        client = self._client_for(self.current_account)
        if not client:
            return
        seen_now = any(m.uid == msg.uid and m.seen for m in self.messages)
        try:
            client.mark(self.current_folder, msg.uid, "\\Seen", add=not seen_now)
        except Exception as e:
            self.notify(f"Flag failed: {e}", severity="error")
            return
        self.load_messages(self.current_account, self.current_folder)

    def action_archive_account(self) -> None:
        tree = self.query_one("#accounts", Tree)
        node = tree.cursor_node
        if not node or not node.data:
            return
        username = node.data.get("username") if isinstance(node.data, dict) else None
        if not username:
            return
        if username in self.archived:
            self.archived.discard(username)
            self.notify(f"Unarchived {username}")
        else:
            self.archived.add(username)
            self.notify(f"Archived {username}")
        save_archived(self.archived_path, self.archived)
        self.refresh_accounts()

    def action_toggle_show_archived(self) -> None:
        self.show_archived = not self.show_archived
        self.notify("Showing archived" if self.show_archived else "Hiding archived")
        self.refresh_accounts()

    # ---- send -------------------------------------------------------

    def _from_addr(self) -> str:
        if self.current_account:
            return f"{self.current_account}@{self.domain}"
        return ""

    def _send_callback(self, msg: EmailMessage | None) -> None:
        if msg is None:
            return
        self._send(msg)

    @work(thread=True, exclusive=False)
    def _send(self, msg: EmailMessage) -> None:
        if not self.current_account:
            return
        client = self._client_for(self.current_account)
        if not client:
            return
        try:
            client.send(msg)
        except Exception as e:
            self.call_from_thread(self.notify, f"Send failed: {e}", severity="error")
            return
        self.call_from_thread(self.notify, "Sent.")

    # ---- cleanup ----------------------------------------------------

    def on_unmount(self) -> None:
        for c in self.clients.values():
            try:
                c.close()
            except Exception:
                pass


# --------------------------- formatting helpers -------------------------


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= n:
        return s.ljust(n)
    return s[: n - 1] + "…"


def _just_name(s: str) -> str:
    if "<" in s and ">" in s:
        name = s.split("<", 1)[0].strip().strip('"')
        return name or s
    return s


def _short_date(s: str) -> str:
    import email.utils as eu
    try:
        dt = eu.parsedate_to_datetime(s)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s[:16]


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_URL_RE = re.compile(r'(?:https?://|mailto:)[^\s<>"\'\)\]]+', re.I)
_HREF_RE = re.compile(r'''href\s*=\s*['"]([^'"]+)['"]''', re.I)
_URL_TRAIL = '.,;:!?)\'"]>'


def _extract_links(msg: FullMessage) -> list[str]:
    """Return ordered, deduped http(s)/mailto links from the message."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(url: str) -> None:
        url = url.strip().rstrip(_URL_TRAIL)
        if not url:
            return
        low = url.lower()
        if not (low.startswith("http://") or low.startswith("https://") or low.startswith("mailto:")):
            return
        if url in seen:
            return
        seen.add(url)
        out.append(url)

    if msg.body_html:
        for url in _HREF_RE.findall(msg.body_html):
            _add(url)
        for url in _URL_RE.findall(msg.body_html):
            _add(url)
    for url in _URL_RE.findall(msg.body_text or ""):
        _add(url)
    return out


def _quote_body(msg: FullMessage) -> str:
    raw = msg.body_text or _html_to_text(msg.body_html)
    quoted = "\n".join("> " + line for line in raw.splitlines())
    return f"\n\nOn {msg.date}, {msg.sender} wrote:\n{quoted}\n"
