"""Embedded Django setup and runner for the webmail interface."""

from pathlib import Path


def start(
    domain: str,
    imap_host: str,
    passwords: dict,
    autocreated: dict,
    work_dir: Path,
    vault_path: Path,
    port: int = 8000,
) -> None:
    import secrets
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=True,
            SECRET_KEY=secrets.token_hex(32),
            ALLOWED_HOSTS=["localhost", "127.0.0.1"],
            INSTALLED_APPS=["ign8mail.webmail"],
            TEMPLATES=[{
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": [
                    "django.template.context_processors.request",
                ]},
            }],
            ROOT_URLCONF="ign8mail.webmail.urls",
            WEBMAIL_DOMAIN=domain,
            WEBMAIL_IMAP_HOST=imap_host,
            WEBMAIL_PASSWORDS=passwords,
            WEBMAIL_AUTOCREATED=autocreated,
            WEBMAIL_WORK_DIR=str(work_dir),
            WEBMAIL_VAULT_PATH=str(vault_path),
        )

    import django
    django.setup()

    from django.core.management import call_command
    call_command("runserver", f"127.0.0.1:{port}", "--noreload")
