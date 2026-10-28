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
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import imap_client, threading as _threading
from .imap_client import MessageSummary
from .threading import Thread

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


def _is_webmail_visible(cfg: dict, name: str) -> bool:
    """Autocreated mailboxes appear in webmail only after `ign8mail autocreated
    enable <name>`. Mailboxes not tracked as autocreated (i.e. seeded manually
    via the vault) are always visible."""
    info = cfg["autocreated"].get(name)
    if info is None:
        return True
    return info.get("status") == "enabled"


def _visible_mailboxes(cfg: dict, user: str) -> list[str]:
    """Mailbox local-parts that *user* can see: their own personal + all shared,
    filtered so autocreated entries only appear once explicitly enabled."""
    own = {addr.split("@", 1)[0] for addr in _personal_addresses_for(cfg, user)}
    candidates = own | set(_shared_local_parts(cfg))
    return sorted(name for name in candidates if _is_webmail_visible(cfg, name))


def _is_shared_mailbox(cfg: dict, mailbox: str) -> bool:
    return mailbox not in _all_personal_local_parts(cfg)


def _allowed_senders(cfg: dict, user: str) -> list[str]:
    domain = cfg["domain"]
    own = [a for a in _personal_addresses_for(cfg, user)
           if _is_webmail_visible(cfg, a.split("@", 1)[0])]
    shared = [f"{lp}@{domain}" for lp in _shared_local_parts(cfg)
              if _is_webmail_visible(cfg, lp)]
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


def _sidebar_skeleton(cfg: dict, user: str) -> list[dict]:
    """Fast, no-IMAP metadata for the sidebar skeleton. The unread counts are
    filled in client-side via /api/sidebar/."""
    visible = _visible_mailboxes(cfg, user)
    autocreated = cfg["autocreated"]
    out = []
    for name in visible:
        if not cfg["passwords"].get(name):
            continue
        out.append({
            "name": name,
            "status": _mailbox_status(name, autocreated),
            "is_autocreated": name in autocreated,
            "shared": _is_shared_mailbox(cfg, name),
        })
    return out


def _sidebar_mailboxes(cfg: dict, user: str) -> list[dict]:
    """Reachable mailbox summaries for the sidebar (IMAP-heavy; JSON endpoint)."""
    visible = _visible_mailboxes(cfg, user)
    host = cfg["imap_host"]
    autocreated = cfg["autocreated"]
    overlay_db = cfg["overlay_db"]

    def _check(name: str) -> dict | None:
        pw = cfg["passwords"].get(name)
        if not pw:
            return None
        status = _mailbox_status(name, autocreated)
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


def _base_context(request: HttpRequest, cfg: dict, user: str, active: str = "") -> dict:
    """Build shared context. Also applies any ?mailbox= or ?days= override to
    the session so the picks stick across every page."""
    visible = _visible_mailboxes(cfg, user)
    if "mailbox" in request.GET:
        requested = (request.GET.get("mailbox") or "").strip()
        if not requested:
            request.session.pop("context_mailbox", None)
        elif requested in visible:
            request.session["context_mailbox"] = requested
    if "days" in request.GET:
        d = (request.GET.get("days") or "").strip().lower()
        if d in ("", "all"):
            request.session.pop("window_days", None)
        else:
            try:
                request.session["window_days"] = max(1, int(d))
            except ValueError:
                pass
    ctx_mb = request.session.get("context_mailbox", "")
    if ctx_mb and ctx_mb not in visible:
        request.session.pop("context_mailbox", None)
        ctx_mb = ""
    window_days = request.session.get("window_days")
    return {
        "user": user,
        "domain": cfg["domain"],
        "sidebar_skeleton": _sidebar_skeleton(cfg, user),
        "active_mailbox": active,
        "context_mailbox": ctx_mb,
        "context_mailboxes": visible,
        "window_days": window_days,
    }


def _fetch_all_summaries(
    cfg: dict, user: str, mailboxes: list[str], per_mailbox_limit: int = 50,
) -> list[tuple[str, MessageSummary]]:
    """Return (mailbox, summary) pairs for the given mailboxes, in parallel."""
    host = cfg["imap_host"]
    overlay_db = cfg["overlay_db"]

    def _fetch(name: str) -> list[tuple[str, MessageSummary]]:
        pw = cfg["passwords"].get(name)
        if not pw:
            return []
        shared = _is_shared_mailbox(cfg, name)
        msgs = imap_client.fetch_recent_summaries(
            host, name, pw, limit=per_mailbox_limit,
            shared_for_user=user if shared else "",
            overlay_db=overlay_db if shared else None,
        )
        return [(name, m) for m in msgs]

    out: list[tuple[str, MessageSummary]] = []
    if not mailboxes:
        return out
    with ThreadPoolExecutor(max_workers=min(len(mailboxes), 10)) as pool:
        for batch in pool.map(_fetch, mailboxes):
            out.extend(batch)
    return out


def _thread_to_row(cfg: dict, t: Thread) -> dict:
    root_shared = _is_shared_mailbox(cfg, t.root_mailbox)
    return {
        "root_mailbox": t.root_mailbox,
        "root_uid": t.root_uid,
        "subject": t.subject,
        "senders": ", ".join(t.senders),
        "count": t.count,
        "unseen_count": t.unseen_count,
        "latest_date": t.messages[-1][1].date if t.messages else "",
        "mailboxes": t.mailboxes,
        "shared": root_shared,
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
    ctx = _base_context(request, cfg, user)
    ctx.update({"view_mode": "list"})
    return render(request, "webmail/index.html", ctx)


def _build_inbox_rows(cfg: dict, user: str, mailboxes: list[str], window_days: int | None) -> list[dict]:
    from datetime import datetime, timedelta, timezone

    entries = _fetch_all_summaries(cfg, user, mailboxes)
    threads = _threading.group_threads(entries)
    if window_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        threads = [t for t in threads if t.latest_dt >= cutoff]
    return [_thread_to_row(cfg, t) for t in threads[:200]]


@_require_login
def thread_view(request: HttpRequest, name: str, root_uid: int) -> HttpResponse:
    cfg = _cfg()
    user = request.session["user"]
    if name not in _visible_mailboxes(cfg, user):
        return redirect("index")
    entries = _fetch_all_summaries(cfg, user, [name], per_mailbox_limit=200)
    threads = _threading.group_threads(entries)
    match: Thread | None = None
    for t in threads:
        if t.root_mailbox == name and t.root_uid == root_uid:
            match = t
            break
        if any(mb == name and m.uid == root_uid for mb, m in t.messages):
            match = t
            break
    if match is None:
        return redirect("mailbox", name=name)

    messages = [{
        "mailbox": mb,
        "uid": m.uid,
        "subject": m.subject,
        "sender": m.sender,
        "date": m.date,
        "seen": m.seen,
        "shared": _is_shared_mailbox(cfg, mb),
    } for mb, m in match.messages]

    ctx = _base_context(request, cfg, user, active=name)
    ctx.update({
        "subject": match.subject,
        "count": match.count,
        "unseen_count": match.unseen_count,
        "messages": messages,
        "root_mailbox": match.root_mailbox,
    })
    return render(request, "webmail/thread.html", ctx)


@_require_login
def timeline_view(request: HttpRequest) -> HttpResponse:
    import math
    from datetime import datetime, timedelta, timezone

    cfg = _cfg()
    user = request.session["user"]
    ctx = _base_context(request, cfg, user)
    ctx_mb = ctx["context_mailbox"]
    mailboxes = [ctx_mb] if ctx_mb else ctx["context_mailboxes"]

    entries = _fetch_all_summaries(cfg, user, mailboxes, per_mailbox_limit=100)
    threads = _threading.group_threads(entries)

    window_days = ctx["window_days"]
    cutoff = None
    if window_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        threads = [t for t in threads if t.latest_dt >= cutoff]

    # Filter noise: drop threads with epoch timestamps.
    good = [t for t in threads if t.latest_dt.timestamp() > 0]
    if not good:
        ctx.update({
            "view_mode": "timeline",
            "lanes": [], "months": [], "empty": True,
            "svg_width": 900, "svg_height": 80,
        })
        return render(request, "webmail/timeline.html", ctx)

    latest = max(t.latest_dt for t in good)
    earliest = min(t.latest_dt for t in good)
    if cutoff is not None:
        # Snap the axis to the window so it reads as "last N days" even when
        # only a few threads land inside it.
        earliest = cutoff
        latest = max(latest, datetime.now(timezone.utc))
    if latest - earliest < timedelta(days=1):
        earliest = latest - timedelta(days=1)
    span = (latest - earliest).total_seconds()

    left_pad = 110
    right_pad = 30
    top_pad = 40
    lane_height = 40
    plot_width = 900

    lane_names = mailboxes  # preserve visible order
    lanes = []
    for idx, mb in enumerate(lane_names):
        y = top_pad + idx * lane_height + lane_height / 2
        dots = []
        for t in good:
            if t.root_mailbox != mb:
                continue
            frac = (t.latest_dt - earliest).total_seconds() / span if span else 0.5
            x = left_pad + frac * plot_width
            r = 4 + 2 * math.log2(max(t.count, 1))
            dots.append({
                "x": round(x, 1),
                "y": round(y, 1),
                "r": round(r, 1),
                "subject": t.subject,
                "count": t.count,
                "root_mailbox": t.root_mailbox,
                "root_uid": t.root_uid,
                "date": t.messages[-1][1].date if t.messages else "",
            })
        lanes.append({"name": mb, "y": round(y, 1), "dots": dots})

    # Month labels along the bottom.
    months = []
    cursor = earliest.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= latest:
        frac = (cursor - earliest).total_seconds() / span if span else 0
        if 0 <= frac <= 1:
            months.append({
                "x": round(left_pad + frac * plot_width, 1),
                "label": cursor.strftime("%b %Y"),
            })
        # Advance ~1 month.
        year, month = cursor.year, cursor.month
        month += 1
        if month > 12:
            month = 1
            year += 1
        cursor = cursor.replace(year=year, month=month)

    svg_height = top_pad + len(lane_names) * lane_height + 40

    ctx.update({
        "view_mode": "timeline",
        "lanes": lanes,
        "months": months,
        "empty": False,
        "svg_width": left_pad + plot_width + right_pad,
        "svg_height": svg_height,
        "left_pad": left_pad,
        "plot_width": plot_width,
        "earliest_label": earliest.strftime("%Y-%m-%d"),
        "latest_label": latest.strftime("%Y-%m-%d"),
    })
    return render(request, "webmail/timeline.html", ctx)


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
    ctx = _base_context(request, cfg, user, active=name)
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
    ctx = _base_context(request, cfg, user, active=name)
    ctx.update({
        "name": name,
        "email": f"{name}@{cfg['domain']}",
        "msg": msg,
    })
    return render(request, "webmail/message.html", ctx)


_INLINE_TYPES = {"application/pdf", "text/plain"}


@_require_login
def attachment_view(request: HttpRequest, name: str, uid: int, index: int) -> HttpResponse:
    """Stream one MIME attachment. PDFs/images render inline; everything else
    downloads. Content-Type is trusted from the IMAP source; nosniff prevents
    Chrome from second-guessing it into something executable."""
    from urllib.parse import quote

    cfg = _cfg()
    user = request.session["user"]
    if name not in _visible_mailboxes(cfg, user):
        return redirect("index")
    pw = cfg["passwords"].get(name)
    if not pw:
        return redirect("index")
    result = imap_client.fetch_attachment(cfg["imap_host"], name, pw, uid, index)
    if result is None:
        return redirect("message", name=name, uid=uid)
    filename, content_type, payload = result
    inline = content_type in _INLINE_TYPES or content_type.startswith("image/")
    disposition = "inline" if inline else "attachment"
    quoted = quote(filename, safe="")
    resp = HttpResponse(payload, content_type=content_type or "application/octet-stream")
    resp["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quoted}"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


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
        attachments = request.FILES.getlist("attachments")
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
                    attachments=attachments,
                )
                sent = True
                initial = {"from_addr": default_from, "to": "", "subject": "", "body": ""}
            except Exception as e:
                error = f"Send failed: {e}"

    ctx = _base_context(request, cfg, user)
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
    attachments: list | None = None,
) -> None:
    """Submit via Postfix on 587 with STARTTLS. sender_login_maps does the authz."""
    import mimetypes
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body or "")

    for f in attachments or []:
        data = f.read()
        if not data:
            continue
        ctype = getattr(f, "content_type", "") or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=f.name,
        )

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


# ---- async JSON endpoints -----------------------------------------------


@_require_login
def sidebar_api(request: HttpRequest) -> JsonResponse:
    cfg = _cfg()
    user = request.session["user"]
    return JsonResponse({"mailboxes": _sidebar_mailboxes(cfg, user)})


@_require_login
def inbox_api(request: HttpRequest) -> JsonResponse:
    cfg = _cfg()
    user = request.session["user"]
    visible = _visible_mailboxes(cfg, user)
    requested = (request.GET.get("mailbox") or "").strip()
    if requested and requested in visible:
        mailboxes = [requested]
    else:
        mailboxes = visible
    window_days: int | None = None
    days_raw = (request.GET.get("days") or "").strip().lower()
    if days_raw and days_raw != "all":
        try:
            window_days = max(1, int(days_raw))
        except ValueError:
            window_days = None
    rows = _build_inbox_rows(cfg, user, mailboxes, window_days)
    return JsonResponse({"rows": rows})
