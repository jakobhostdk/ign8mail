"""Cooperative webmail views.

Auth: per-user IMAP-bind. Once authenticated, the session-bound user sees
their own personal mailboxes plus every shared mailbox (anything in the vault's
mailbox_passwords that isn't in another user's personal_addresses list).

Read state for shared mailboxes lives in a SQLite overlay, so each user has
their own unread count without flipping IMAP \\Seen for everyone else.
"""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import imap_client

# ---- shared context helpers ---------------------------------------------


def _cfg() -> dict:
    return {
        "domain": settings.WEBMAIL_DOMAIN,
        "imap_host": settings.WEBMAIL_IMAP_HOST,
        "smtp_host": getattr(settings, "WEBMAIL_SMTP_HOST", settings.WEBMAIL_IMAP_HOST),
        "passwords": settings.WEBMAIL_PASSWORDS,
        "autocreated": settings.WEBMAIL_AUTOCREATED,
        "users": getattr(settings, "WEBMAIL_USERS", {}) or {},
        "work_dir": Path(settings.WEBMAIL_WORK_DIR),
        "vault_path": Path(settings.WEBMAIL_VAULT_PATH),
        "overlay_db": Path(settings.WEBMAIL_OVERLAY_DB),
    }


def _personal_addresses_for(cfg: dict, user: str) -> list[str]:
    return list((cfg["users"].get(user) or {}).get("personal_addresses") or [])


def _all_personal_local_parts(cfg: dict) -> set[str]:
    out: set[str] = set()
    for u in cfg["users"].values():
        for addr in (u.get("personal_addresses") or []):
            out.add(addr.split("@", 1)[0])
    return out


def _shared_local_parts(cfg: dict) -> list[str]:
    personal = _all_personal_local_parts(cfg)
    return sorted(m for m in cfg["passwords"].keys() if m not in personal)


def _visible_mailboxes(cfg: dict, user: str) -> list[str]:
    """Mailbox local-parts that *user* can see: their own personal + all shared."""
    own = {addr.split("@", 1)[0] for addr in _personal_addresses_for(cfg, user)}
    return sorted(own | set(_shared_local_parts(cfg)))


def _is_shared_mailbox(cfg: dict, mailbox: str) -> bool:
    return mailbox not in _all_personal_local_parts(cfg)


def _allowed_senders(cfg: dict, user: str) -> list[str]:
    domain = cfg["domain"]
    own = _personal_addresses_for(cfg, user)
    shared = [f"{lp}@{domain}" for lp in _shared_local_parts(cfg)]
    return sorted(set(own + shared))


def _mailbox_status(name: str, autocreated: dict) -> str:
    info = autocreated.get(name)
    if info is None:
        return "active"
    return info.get("status", "pending")


# ---- auth wrapper -------------------------------------------------------


def _require_login(view_fn: Callable) -> Callable:  # type: ignore[type-arg]
    @wraps(view_fn)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:  # type: ignore[no-untyped-def]
        if not request.session.get("user"):
            return redirect("login")
        return view_fn(request, *args, **kwargs)
    return wrapper


# ---- sidebar ------------------------------------------------------------


def _sidebar_mailboxes(cfg: dict, user: str) -> list[dict]:
    """Reachable mailbox summaries for the sidebar."""
    visible = _visible_mailboxes(cfg, user)
    host = cfg["imap_host"]
    autocreated = cfg["autocreated"]
    overlay_db = cfg["overlay_db"]

    def _check(name: str) -> dict | None:
        pw = cfg["passwords"].get(name)
        if not pw:
            return None
        status = _mailbox_status(name, autocreated)
        if status == "silenced":
            return None
        shared = _is_shared_mailbox(cfg, name)
        unread = imap_client.unread_count(
            host, name, pw,
            shared_for_user=user if shared else "",
            overlay_db=overlay_db if shared else None,
        )
        return {
            "name": name,
            "unread": unread,
            "status": status,
            "reachable": unread >= 0,
            "is_autocreated": name in autocreated,
            "shared": shared,
        }

    if not visible:
        return []
    with ThreadPoolExecutor(max_workers=min(len(visible), 10)) as pool:
        result = [mb for mb in pool.map(_check, visible) if mb is not None]
    result.sort(key=lambda m: (-m["unread"] if m["reachable"] else 0, m["name"]))
    return result


def _base_context(cfg: dict, user: str, active: str = "") -> dict:
    return {
        "user": user,
        "domain": cfg["domain"],
        "sidebar_mailboxes": _sidebar_mailboxes(cfg, user),
        "active_mailbox": active,
    }


# ---- views --------------------------------------------------------------


def login_view(request: HttpRequest) -> HttpResponse:
    if request.session.get("user"):
        return redirect("index")

    cfg = _cfg()
    if not cfg["users"]:
        return render(request, "webmail/login.html", {
            "error": "No users registered. Run `ign8mail user-add <username>` first.",
            "domain": cfg["domain"],
        })

    error = ""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip().split("@", 1)[0]
        password = request.POST.get("password") or ""
        if username and password and username in cfg["users"]:
            if _verify_imap_login(cfg["imap_host"], username, password):
                request.session["user"] = username
                return redirect("index")
        error = "Wrong username or password."
    return render(request, "webmail/login.html", {"error": error, "domain": cfg["domain"]})


def _verify_imap_login(host: str, username: str, password: str) -> bool:
    try:
        imap = imap_client._connect(host, username, password)
        imap.logout()
        return True
    except Exception:
        return False


def logout_view(request: HttpRequest) -> HttpResponse:
    request.session.flush()
    return redirect("login")


@_require_login
def unified_inbox(request: HttpRequest) -> HttpResponse:
    cfg = _cfg()
    user = request.session["user"]
    visible = _visible_mailboxes(cfg, user)
    overlay_db = cfg["overlay_db"]
    host = cfg["imap_host"]

    def _fetch(name: str) -> list[dict]:
        pw = cfg["passwords"].get(name)
        if not pw:
            return []
        if _mailbox_status(name, cfg["autocreated"]) == "silenced":
            return []
        shared = _is_shared_mailbox(cfg, name)
        msgs = imap_client.fetch_recent_summaries(
            host, name, pw, limit=30,
            shared_for_user=user if shared else "",
            overlay_db=overlay_db if shared else None,
        )
        return [{
            "mailbox": name,
            "uid": m.uid,
            "subject": m.subject,
            "sender": m.sender,
            "date": m.date,
            "seen": m.seen,
            "shared": shared,
        } for m in msgs]

    rows: list[dict] = []
    if visible:
        with ThreadPoolExecutor(max_workers=min(len(visible), 10)) as pool:
            for batch in pool.map(_fetch, visible):
                rows.extend(batch)
    rows.sort(key=lambda r: r["date"], reverse=True)

    ctx = _base_context(cfg, user)
    ctx.update({"rows": rows[:200]})
    return render(request, "webmail/index.html", ctx)


@_require_login
def mailbox_view(request: HttpRequest, name: str) -> HttpResponse:
    cfg = _cfg()
    user = request.session["user"]
    if name not in _visible_mailboxes(cfg, user):
        return redirect("index")
    pw = cfg["passwords"].get(name)
    if not pw:
        return redirect("index")
    shared = _is_shared_mailbox(cfg, name)
    messages = imap_client.list_messages(
        cfg["imap_host"], name, pw,
        shared_for_user=user if shared else "",
        overlay_db=cfg["overlay_db"] if shared else None,
    )
    status = _mailbox_status(name, cfg["autocreated"])
    ctx = _base_context(cfg, user, active=name)
    ctx.update({
        "name": name,
        "email": f"{name}@{cfg['domain']}",
        "messages": messages,
        "status": status,
        "is_autocreated": name in cfg["autocreated"],
        "shared": shared,
    })
    return render(request, "webmail/mailbox.html", ctx)


@_require_login
def message_view(request: HttpRequest, name: str, uid: int) -> HttpResponse:
    cfg = _cfg()
    user = request.session["user"]
    if name not in _visible_mailboxes(cfg, user):
        return redirect("index")
    pw = cfg["passwords"].get(name)
    if not pw:
        return redirect("index")
    shared = _is_shared_mailbox(cfg, name)
    msg = imap_client.fetch_message(
        cfg["imap_host"], name, pw, uid,
        shared_for_user=user if shared else "",
        overlay_db=cfg["overlay_db"] if shared else None,
    )
    if not msg:
        return redirect("mailbox", name=name)
    ctx = _base_context(cfg, user, active=name)
    ctx.update({
        "name": name,
        "email": f"{name}@{cfg['domain']}",
        "msg": msg,
    })
    return render(request, "webmail/message.html", ctx)


# ---- compose ------------------------------------------------------------


@_require_login
def compose_view(request: HttpRequest) -> HttpResponse:
    cfg = _cfg()
    user = request.session["user"]
    allowed = _allowed_senders(cfg, user)
    default_from = _personal_addresses_for(cfg, user)
    default_from = default_from[0] if default_from else (allowed[0] if allowed else "")

    error = ""
    sent = False
    initial = {
        "from_addr": (
            request.POST.get("from_addr", "")
            or request.GET.get("reply_from", "")
            or default_from
        ),
        "to": request.POST.get("to", "") or request.GET.get("to", ""),
        "subject": request.POST.get("subject", "") or request.GET.get("subject", ""),
        "body": request.POST.get("body", "") or "",
    }

    if request.method == "POST":
        from_addr = initial["from_addr"]
        to = initial["to"].strip()
        subject = initial["subject"]
        body = initial["body"]
        if from_addr not in allowed:
            error = "You are not allowed to send from that address."
        elif not to:
            error = "Recipient required."
        else:
            try:
                _send_mail(
                    smtp_host=cfg["smtp_host"],
                    sasl_user=user,
                    sasl_password=cfg["passwords"].get(user, ""),
                    from_addr=from_addr,
                    to=to,
                    subject=subject,
                    body=body,
                )
                sent = True
                initial = {"from_addr": default_from, "to": "", "subject": "", "body": ""}
            except Exception as e:
                error = f"Send failed: {e}"

    ctx = _base_context(cfg, user)
    ctx.update({
        "allowed_senders": allowed,
        "initial": initial,
        "error": error,
        "sent": sent,
    })
    return render(request, "webmail/compose.html", ctx)


def _send_mail(
    smtp_host: str,
    sasl_user: str,
    sasl_password: str,
    from_addr: str,
    to: str,
    subject: str,
    body: str,
) -> None:
    """Submit via Postfix on 587 with STARTTLS. sender_login_maps does the authz."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body or "")

    with smtplib.SMTP(smtp_host, 587, timeout=30) as s:
        s.starttls()
        s.login(sasl_user, sasl_password)
        s.send_message(msg, from_addr=from_addr, to_addrs=[to])


# ---- mailbox lifecycle actions (silence / disable) ----------------------


@require_POST
@_require_login
def mailbox_action(request: HttpRequest, name: str) -> HttpResponse:
    cfg = _cfg()
    user = request.session["user"]
    if name not in _visible_mailboxes(cfg, user) or not _is_shared_mailbox(cfg, name):
        return redirect("index")

    action = request.POST.get("action")
    if action not in ("silence", "disable"):
        return redirect("index")

    if getattr(settings, "WEBMAIL_SERVER_MODE", False):
        return _mailbox_action_server(name, action)
    return _mailbox_action_client(name, action)


def _mailbox_action_server(name: str, action: str) -> HttpResponse:
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
            from ign8mail import vault as _vault
            data = _vault.load(vault_path)
            entries = data.setdefault("autocreated_mailboxes", {})
            if action == "silence":
                entries.pop(name, None)
                entries[name] = {"status": "silenced"}
                data.get("mailbox_passwords", {}).pop(name, None)
                settings.WEBMAIL_PASSWORDS.pop(name, None)
            elif action == "disable":
                entries.setdefault(name, {})["status"] = "disabled"
            data["autocreated_mailboxes"] = entries
            _vault.save(data, vault_path)
            settings.WEBMAIL_AUTOCREATED = entries
        except Exception:
            pass

    return redirect("index")


# Used in templates only — kept here for url-reverse convenience.
_ = reverse
