"""Hetzner Cloud provisioner: SSH key, firewall, and server via the hcloud SDK."""

import json
from pathlib import Path

from hcloud import Client
from hcloud.firewalls.domain import FirewallRule
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.server_types import ServerType

from ign8mail.config import Config

_NAME = "ign8mail"
_SSH_KEY_NAME = "ign8mail-key"
_FW_NAME = "ign8mail-fw"

_INBOUND_PORTS = ["22", "25", "80", "443", "465", "587", "993"]


def provision(cfg: Config, public_key: str, state_path: Path) -> dict[str, str]:
    client = Client(token=cfg.hetzner_token)

    ssh_key = _ensure_ssh_key(client, public_key)
    firewall = _ensure_firewall(client)
    server = _ensure_server(client, cfg, ssh_key, firewall)

    import ipaddress as _ipaddress
    _ipv4 = server.public_net.ipv4.ip if server.public_net.ipv4 else ""
    _ipv6 = ""
    if server.public_net.ipv6:
        _net = _ipaddress.IPv6Network(server.public_net.ipv6.ip)
        _ipv6 = str(_net.network_address + 1)

    _mail_hostname = f"mail.{cfg.domain}"
    if _ipv4:
        server.change_dns_ptr(ip=_ipv4, dns_ptr=_mail_hostname).wait_until_finished()
    if _ipv6:
        server.change_dns_ptr(ip=_ipv6, dns_ptr=_mail_hostname).wait_until_finished()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "server_id": server.id,
        "ssh_key_id": ssh_key.id,
        "firewall_id": firewall.id,
        "ipv4": _ipv4,
        "ipv6": _ipv6,
    }, indent=2))

    return {"ipv4": _ipv4, "ipv6": _ipv6}


def destroy(state_path: Path, hetzner_token: str) -> None:
    state = json.loads(state_path.read_text())
    client = Client(token=hetzner_token)

    for server in client.servers.get_all(name=_NAME):
        action = server.delete()
        action.wait_until_finished()

    for fw in client.firewalls.get_all(name=_FW_NAME):
        fw.delete()

    for key in client.ssh_keys.get_all(name=_SSH_KEY_NAME):
        key.delete()

    state_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------

def _ensure_ssh_key(client: Client, public_key: str):  # type: ignore[return]
    existing = client.ssh_keys.get_all(name=_SSH_KEY_NAME)
    if existing:
        key = existing[0]
        if key.public_key.strip() != public_key.strip():
            key.delete()
        else:
            return key
    return client.ssh_keys.create(name=_SSH_KEY_NAME, public_key=public_key)


def _ensure_firewall(client: Client):  # type: ignore[return]
    existing = client.firewalls.get_all(name=_FW_NAME)
    if existing:
        return existing[0]
    rules = [
        FirewallRule(
            direction="in",
            protocol="tcp",
            port=port,
            source_ips=["0.0.0.0/0", "::/0"],
        )
        for port in _INBOUND_PORTS
    ]
    return client.firewalls.create(name=_FW_NAME, rules=rules).firewall


def _ensure_server(client: Client, cfg: Config, ssh_key, firewall):  # type: ignore[return]
    existing = client.servers.get_all(name=_NAME)
    if existing:
        return existing[0]
    response = client.servers.create(
        name=_NAME,
        server_type=ServerType(name=cfg.server_type),
        image=Image(name=cfg.server_image),
        location=Location(name=cfg.server_location),
        ssh_keys=[ssh_key],
        firewalls=[firewall],
        labels={"managed-by": "ign8mail", "domain": cfg.domain},
    )
    response.action.wait_until_finished()
    return response.server
