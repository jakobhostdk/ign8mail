"""Views for the webmail interface."""

import json
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import imap_client


def _cfg() -> dict:
    return {
        "domain": settings.WEBMAIL_DOMAIN,
        "imap_host": settings.WEBMAIL_IMAP_HOST,
        "passwords": settings.WEBMAIL_PASSWORDS,
        "autocreated": settings.WEBMAIL_AUTOCREATED,
        "work_dir": Path(settings.WEBMAIL_WORK_DIR),
        "vault_path": Path(settings.WEBMAIL_VAULT_PATH),
    }


def _mailbox_status(name: str, autocreated: dict) -> str:
    info = autocreated.get(name)
    if info is None:
        return "active"
    return info.get("status", "pending")


def _sidebar_mailboxes(cfg: dict) -> list[dict]:
    result = []
    for name, pw in sorted(cfg["passwords"].items()):
        status = _mailbox_status(name, cfg["autocreated"])
        if status == "silenced":
            continue
        unread = imap_client.unread_count(cfg["imap_host"], name, pw)
        result.append({
            "name": name,
            "unread": unread,
            "status": status,
            "reachable": unread >= 0,
            "is_autocreated": name in cfg["autocreated"],
        })
    result.sort(key=lambda m: (-m["unread"] if m["reachable"] else 0, m["name"]))
    return result


def index(request: HttpRequest) -> HttpResponse:
    cfg = _cfg()
    mailboxes = _sidebar_mailboxes(cfg)
    return render(request, "webmail/index.html", {
        "mailboxes": mailboxes,
        "sidebar_mailboxes": mailboxes,
        "domain": cfg["domain"],
    })


def mailbox_view(request: HttpRequest, name: str) -> HttpResponse:
    cfg = _cfg()
    pw = cfg["passwords"].get(name)
    if not pw:
        return redirect("index")
    messages = imap_client.list_messages(cfg["imap_host"], name, pw)
    status = _mailbox_status(name, cfg["autocreated"])
    return render(request, "webmail/mailbox.html", {
        "name": name,
        "email": f"{name}@{cfg['domain']}",
        "messages": messages,
        "status": status,
        "is_autocreated": name in cfg["autocreated"],
        "domain": cfg["domain"],
        "sidebar_mailboxes": _sidebar_mailboxes(cfg),
        "active_mailbox": name,
    })


def message_view(request: HttpRequest, name: str, seq: str) -> HttpResponse:
    cfg = _cfg()
    pw = cfg["passwords"].get(name)
    if not pw:
        return redirect("index")
    msg = imap_client.fetch_message(cfg["imap_host"], name, pw, seq)
    if not msg:
        return redirect("mailbox", name=name)
    return render(request, "webmail/message.html", {
        "name": name,
        "email": f"{name}@{cfg['domain']}",
        "msg": msg,
        "domain": cfg["domain"],
        "sidebar_mailboxes": _sidebar_mailboxes(cfg),
        "active_mailbox": name,
    })


@require_POST
def mailbox_action(request: HttpRequest, name: str) -> HttpResponse:
    action = request.POST.get("action")
    if action not in ("silence", "disable"):
        return redirect("index")

    cfg = _cfg()
    work_dir = cfg["work_dir"]
    state_path = work_dir / "state.json"
    if not state_path.exists():
        return redirect("index")

    state = json.loads(state_path.read_text())
    ipv4 = state.get("ipv4", "")
    priv_key = work_dir / "keys" / "ign8mail"
    if not ipv4 or not priv_key.exists():
        return redirect("index")

    from ign8mail.config import Config
    from ign8mail.mailserver.setup import MailServerSetup

    placeholder = Config(
        domain="placeholder",
        admin_email="placeholder@placeholder.com",  # type: ignore[arg-type]
        hetzner_token="placeholder",
        cloudflare_token="placeholder",
        cloudflare_zone_id="placeholder",
    )
    setup = MailServerSetup(placeholder, host=ipv4, private_key_path=str(priv_key))
    try:
        setup.connect(retries=2, delay=3)
        if action == "silence":
            setup.silence_user(name)
        elif action == "disable":
            setup.disable_user(name)
        setup.disconnect()
    except Exception:
        pass

    vault_path = cfg["vault_path"]
    if vault_path.exists():
        try:
            from ign8mail import vault
            data = vault.load(vault_path)
            entries = data.setdefault("autocreated_mailboxes", {})
            if action == "silence":
                entries.pop(name, None)
                entries[name] = {"status": "silenced"}
                data.get("mailbox_passwords", {}).pop(name, None)
                settings.WEBMAIL_PASSWORDS.pop(name, None)
            elif action == "disable":
                entries.setdefault(name, {})["status"] = "disabled"
            data["autocreated_mailboxes"] = entries
            vault.save(data, vault_path)
            settings.WEBMAIL_AUTOCREATED = entries
        except Exception:
            pass

    return redirect("index")
