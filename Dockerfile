# Cooperative webmail (Django) served by gunicorn behind Traefik.
# Only the webmail runs in the container — the Postfix/Dovecot/rspamd stack
# still lives on the Hetzner mail VPS provisioned by `ign8mail up`.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY webmail_wsgi.py ./

RUN pip install . gunicorn>=22 \
    && mkdir -p /var/lib/ign8mail

EXPOSE 8000

# 2 workers is enough — the whole app is IMAP-bound to a single upstream server.
# Gevent would help concurrency but adds a native dep; keep it simple.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", \
     "--access-logfile", "-", "--error-logfile", "-", "webmail_wsgi:application"]
