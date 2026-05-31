"""Views for the webmail interface."""

import json
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import imap_client


def _require_login(view_fn):  # type: ignore[no-untyped-def]
    @wraps(view_fn)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:  # type: ignore[no-untyped-def]
        if not request.session.get("ok"):
            return redirect("login")
        return view_fn(request, *args, **kwargs)
    return wrapper


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
    from concurrent.futures import ThreadPoolExecutor

    host = cfg["imap_host"]
    autocreated = cfg["autocreated"]

    def _check(name: str, pw: str) -> dict | None:
        status = _mailbox_status(name, autocreated)
        if status == "silenced":
            return None
        unread = imap_client.unread_count(host, name, pw)
        return {
            "name": name,
            "unread": unread,
            "status": status,
            "reachable": unread >= 0,
            "is_autocreated": name in autocreated,
        }

    items = list(cfg["passwords"].items())
    with ThreadPoolExecutor(max_workers=min(len(items), 10)) as pool:
        result = [mb for mb in pool.map(lambda p: _check(*p), items) if mb is not None]

    result.sort(key=lambda m: (-m["unread"] if m["reachable"] else 0, m["name"]))
    return result


def login_view(request: HttpRequest) -> HttpResponse:
    if request.session.get("ok"):
        return redirect("index")
    error = ""
    if request.method == "POST":
        pw = request.POST.get("password", "")
        admin_pw = getattr(settings, "WEBMAIL_ADMIN_PASSWORD", "")
        if admin_pw and pw == admin_pw:
            request.session["ok"] = True
            return redirect("index")
        error = "Wrong password."
    return render(request, "webmail/login.html", {"error": error})


def logout_view(request: HttpRequest) -> HttpResponse:
    request.session.flush()
    return redirect("login")


@_require_login
def index(request: HttpRequest) -> HttpResponse:
    cfg = _cfg()
    mailboxes = _sidebar_mailboxes(cfg)
    return render(request, "webmail/index.html", {
        "mailboxes": mailboxes,
        "sidebar_mailboxes": mailboxes,
        "domain": cfg["domain"],
    })


@_require_login
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


@_require_login
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
@_require_login
def mailbox_action(request: HttpRequest, name: str) -> HttpResponse:
    action = request.POST.get("action")
    if action not in ("silence", "disable"):
        return redirect("index")

    if getattr(settings, "WEBMAIL_SERVER_MODE", False):
        return _mailbox_action_server(name, action)
    return _mailbox_action_client(name, action)


def _mailbox_action_server(name: str, action: str) -> HttpResponse:
    """Run disable/silence directly on the server via subprocess."""
    import subprocess

    if action in ("disable", "silence"):
        subprocess.run(
            f"sed -i '/^{name} /d' /etc/postfix/local_recipients && "
            "postmap /etc/postfix/local_recipients && systemctl reload postfix",
            shell=True, check=False,
        )
    if action == "silence":
        subprocess.run(
            f"grep -qxF '{name}' /etc/postfix/silenced_recipients || "
            f"echo '{name}' >> /etc/postfix/silenced_recipients",
            shell=True, check=False,
        )
        subprocess.run(["userdel", "-r", name], check=False)

    config_path = Path(settings.WEBMAIL_VAULT_PATH)
    if config_path.exists():
        data = json.loads(config_path.read_text())
        if action == "silence":
            data.get("passwords", {}).pop(name, None)
            settings.WEBMAIL_PASSWORDS.pop(name, None)
        data.setdefault("autocreated", {})[name] = {
            "status": "silenced" if action == "silence" else "disabled"
        }
        config_path.write_text(json.dumps(data, indent=2))
        settings.WEBMAIL_AUTOCREATED = data.get("autocreated", {})

    return redirect("index")


def _mailbox_action_client(name: str, action: str) -> HttpResponse:
    """SSH into the remote server to run disable/silence, then update the vault."""
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
