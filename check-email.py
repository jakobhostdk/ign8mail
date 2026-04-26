#!/usr/bin/env python3
"""
check-email <address>

Traces where an email address routes to and whether it's likely to exist.

Steps:
  1. Resolve MX records for the domain
  2. Connect to each mail server (port 25) in priority order
  3. Run an SMTP probe (EHLO → MAIL FROM → RCPT TO) and report the response
"""

import smtplib
import socket
import subprocess
import sys
from dataclasses import dataclass


PROBE_FROM = "probe@jakobholst.dk"
TIMEOUT = 10


@dataclass
class MX:
    priority: int
    host: str


def resolve_mx(domain: str) -> list[MX]:
    try:
        out = subprocess.check_output(
            ["dig", "+short", "MX", domain],
            text=True, timeout=10,
        )
    except subprocess.CalledProcessError:
        return []

    records = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                records.append(MX(priority=int(parts[0]), host=parts[1].rstrip(".")))
            except ValueError:
                pass
    return sorted(records, key=lambda r: r.priority)


def resolve_a(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        return "unresolvable"


def smtp_probe(mx_host: str, email: str) -> tuple[int, str]:
    """
    Returns (smtp_code, message).
    Connects, sends EHLO + MAIL FROM + RCPT TO, then RSET + QUIT.
    """
    try:
        with smtplib.SMTP(timeout=TIMEOUT) as s:
            s.connect(mx_host, 25)
            s.ehlo_or_helo_if_needed()
            s.mail("")
            code, msg = s.rcpt(email)
            s.rset()
            return code, msg.decode(errors="replace")
    except smtplib.SMTPConnectError as e:
        return e.smtp_code, str(e.smtp_error)
    except smtplib.SMTPServerDisconnected:
        return 0, "server disconnected unexpectedly"
    except smtplib.SMTPException as e:
        return 0, str(e)
    except (OSError, TimeoutError) as e:
        return 0, str(e)


def verdict(code: int) -> str:
    if code == 250:
        return "✓ accepted"
    if code == 251:
        return "→ forwarded (address redirects elsewhere)"
    if code in (550, 551, 553):
        return "✗ rejected (address does not exist)"
    if code == 552:
        return "✗ rejected (mailbox full)"
    if code in (421, 450, 451, 452):
        return "? temporarily unavailable (try later)"
    if code == 454:
        return "~ relay denied (server requires auth — address likely valid)"
    if code == 0:
        return "? connection failed"
    return f"? unknown response ({code})"


def main() -> None:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        print(f"Usage: {sys.argv[0]} user@domain")
        sys.exit(1)

    email = sys.argv[1].strip()
    _, domain = email.rsplit("@", 1)

    print(f"\nChecking: {email}")
    print(f"Domain:   {domain}")
    print()

    mx_records = resolve_mx(domain)
    if not mx_records:
        print("  ✗ No MX records found — domain cannot receive email")
        sys.exit(1)

    print(f"  MX records ({len(mx_records)} found, in priority order):")
    for mx in mx_records:
        ip = resolve_a(mx.host)
        print(f"    [{mx.priority:3d}]  {mx.host}  ({ip})")

    print()
    print("  SMTP probe:")

    for mx in mx_records:
        print(f"    → {mx.host}:25 ... ", end="", flush=True)
        code, msg = smtp_probe(mx.host, email)
        v = verdict(code)
        print(f"{v}")
        if msg:
            print(f"         server said: {msg.strip()}")

        # Stop after first server that gives a definitive answer
        if code in (250, 251, 550, 551, 552, 553):
            break

    print()


if __name__ == "__main__":
    main()
