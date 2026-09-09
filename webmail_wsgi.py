"""Production WSGI entrypoint for the cooperative webmail (swarm deploy).

Config is read from a JSON file at $WEBMAIL_CONFIG_PATH (default
`/run/secrets/webmail_config`). The file matches the shape the SSH-deployed
webmail already uses — same keys, just rendered from the vault by the operator
before `docker stack deploy`:

    {
      "domain":     "jakobholst.dk",
      "imap_host":  "mail.jakobholst.dk",
      "smtp_host":  "mail.jakobholst.dk",
      "passwords":  { "<mailbox>": "<pw>", ... },
      "autocreated": { ... },
      "users":       { "<user>": { "personal_addresses": [...] } }
    }

Django SECRET_KEY is loaded from `/run/secrets/django_secret_key`. ALLOWED_HOSTS
comes from $WEBMAIL_ALLOWED_HOSTS (comma-separated).
"""

import json
import os
from pathlib import Path

from django.conf import settings
from django.core.wsgi import get_wsgi_application

_CONFIG_PATH = Path(os.environ.get("WEBMAIL_CONFIG_PATH", "/run/secrets/webmail_config"))
_SECRET_KEY_PATH = Path(os.environ.get("WEBMAIL_SECRET_KEY_PATH", "/run/secrets/django_secret_key"))
_WORK_DIR = Path(os.environ.get("WEBMAIL_WORK_DIR", "/var/lib/ign8mail"))

_cfg = json.loads(_CONFIG_PATH.read_text())
_allowed = [h.strip() for h in os.environ.get("WEBMAIL_ALLOWED_HOSTS", "").split(",") if h.strip()]

settings.configure(
    DEBUG=False,
    SECRET_KEY=_SECRET_KEY_PATH.read_text().strip(),
    ALLOWED_HOSTS=_allowed or ["*"],
    INSTALLED_APPS=["ign8mail.webmail"],
    MIDDLEWARE=[
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
    ],
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    TEMPLATES=[{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }],
    ROOT_URLCONF="ign8mail.webmail.urls",
    WEBMAIL_DOMAIN=_cfg["domain"],
    WEBMAIL_IMAP_HOST=_cfg["imap_host"],
    WEBMAIL_SMTP_HOST=_cfg.get("smtp_host") or _cfg["imap_host"],
    WEBMAIL_PASSWORDS=_cfg.get("passwords") or {},
    WEBMAIL_AUTOCREATED=_cfg.get("autocreated") or {},
    WEBMAIL_USERS=_cfg.get("users") or {},
    WEBMAIL_WORK_DIR=str(_WORK_DIR),
    WEBMAIL_VAULT_PATH=str(_CONFIG_PATH),
    WEBMAIL_OVERLAY_DB=str(_WORK_DIR / "seen.db"),
    WEBMAIL_SERVER_MODE=False,
)

application = get_wsgi_application()
