"""Create all DNS records required for a production mail server."""

import cloudflare
from cloudflare.types.dns import RecordCreateParams


class CloudflareDNS:
    def __init__(self, token: str, zone_id: str, domain: str, email: str = "") -> None:
        if email:
            self._cf = cloudflare.Cloudflare(api_email=email, api_key=token)
        else:
            self._cf = cloudflare.Cloudflare(api_token=token)
        self._zone = zone_id
        self._domain = domain

    def provision(self, ipv4: str, ipv6: str, dkim_public_key: str) -> None:
        records: list[RecordCreateParams] = [
            # A / AAAA for the mail hostname
            {"type": "A",    "name": f"mail.{self._domain}", "content": ipv4,   "proxied": False, "ttl": 1},
            {"type": "AAAA", "name": f"mail.{self._domain}", "content": ipv6,   "proxied": False, "ttl": 1},
            # MX
            {"type": "MX",   "name": self._domain,           "content": f"mail.{self._domain}", "priority": 10, "ttl": 1},
            # SPF
            {"type": "TXT",  "name": self._domain,           "content": f"v=spf1 mx a:{ipv4} ~all", "ttl": 1},
            # DKIM
            {"type": "TXT",  "name": f"mail._domainkey.{self._domain}", "content": f"v=DKIM1; k=rsa; p={dkim_public_key}", "ttl": 1},
            # DMARC
            {"type": "TXT",  "name": f"_dmarc.{self._domain}", "content": f"v=DMARC1; p=quarantine; rua=mailto:dmarc@{self._domain}", "ttl": 1},
            # Autodiscover / autoconfig for mail clients
            {"type": "CNAME", "name": f"autoconfig.{self._domain}",  "content": f"mail.{self._domain}", "proxied": False, "ttl": 1},
            {"type": "CNAME", "name": f"autodiscover.{self._domain}", "content": f"mail.{self._domain}", "proxied": False, "ttl": 1},
        ]

        names = {r["name"] for r in records}  # type: ignore[index]
        for existing in self._cf.dns.records.list(zone_id=self._zone).result or []:
            if existing.name in names:
                self._cf.dns.records.delete(existing.id, zone_id=self._zone)

        for record in records:
            self._cf.dns.records.create(zone_id=self._zone, **record)  # type: ignore[arg-type]
