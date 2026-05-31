"""Create all DNS records required for a production mail server."""

import datetime

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

    def provision_webmail(self, ipv4: str) -> tuple[bytes, bytes, bool]:
        """Add webmail.{domain} A record (Cloudflare-proxied) and create an origin cert.

        Returns (cert_pem, key_pem, origin_cert_used).
        Tries Cloudflare origin certificate first; falls back to a 10-year self-signed cert
        if the API token lacks the SSL permission (Cloudflare SSL mode must be 'Full' then).
        """
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        # webmail subdomain, proxied through Cloudflare
        self._upsert("A", f"webmail.{self._domain}", ipv4, proxied=True)

        # Generate the private key (used for both the CSR and any self-signed fallback)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )

        hostnames = [f"webmail.{self._domain}", f"*.{self._domain}"]

        # Build a CSR to submit to Cloudflare
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, f"webmail.{self._domain}"),
            ]))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(h) for h in hostnames]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()

        try:
            origin_cert = self._cf.origin_ca_certificates.create(
                csr=csr_pem,
                hostnames=hostnames,
                request_type="origin-rsa",
                requested_validity=5475,  # 15 years
            )
            cert_pem = origin_cert.certificate.encode()
            origin_cert_used = True
        except Exception:
            # API token lacks SSL/Certificates permission — fall back to self-signed.
            # Cloudflare SSL mode must be set to "Full" (not strict) in the dashboard.
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, f"webmail.{self._domain}"),
                ]))
                .issuer_name(x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, f"webmail.{self._domain}"),
                ]))
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(f"webmail.{self._domain}")]),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )
            cert_pem = cert.public_bytes(serialization.Encoding.PEM)
            origin_cert_used = False

        return cert_pem, key_pem, origin_cert_used

    def _upsert(self, rtype: str, name: str, content: str, *, proxied: bool = False) -> None:
        """Delete any existing record with the same type+name, then create."""
        for existing in self._cf.dns.records.list(zone_id=self._zone).result or []:
            if existing.type == rtype and existing.name == name:
                self._cf.dns.records.delete(existing.id, zone_id=self._zone)
        self._cf.dns.records.create(  # type: ignore[arg-type]
            zone_id=self._zone,
            type=rtype,
            name=name,
            content=content,
            proxied=proxied,
            ttl=1,
        )
