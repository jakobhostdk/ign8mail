# DNS records

`CloudflareDNS.provision()` creates every record needed for production mail in one call. All records are created in the Cloudflare zone identified by `IGN8_CLOUDFLARE_ZONE_ID`.

## Records created

| Type | Name | Content | Proxied |
|------|------|---------|---------|
| **A** | `mail.<domain>` | server IPv4 | no |
| **AAAA** | `mail.<domain>` | server IPv6 | no |
| **MX** | `<domain>` | `mail.<domain>` (priority 10) | n/a |
| **TXT** (SPF) | `<domain>` | `v=spf1 mx a:<ipv4> ~all` | n/a |
| **TXT** (DKIM) | `mail2._domainkey.<domain>` | `v=DKIM1; k=rsa; p=<public-key>` | n/a |
| **TXT** (DMARC) | `_dmarc.<domain>` | `v=DMARC1; p=quarantine; rua=mailto:dmarc@<domain>` | n/a |
| **CNAME** | `autoconfig.<domain>` | `mail.<domain>` | no |
| **CNAME** | `autodiscover.<domain>` | `mail.<domain>` | no |

!!! note "DKIM selector"
    The selector is `mail2` (not `mail`) — the change was made in v2.3.0. If you rotate keys, the old `mail._domainkey.<domain>` record is left untouched.

!!! note "Cloudflare proxy"
    Mail records (`mail.<domain>`, autoconfig CNAMEs) are **never** proxied — Cloudflare does not proxy SMTP/IMAP and would break TLS. SPF/DKIM/DMARC are TXT records and aren't proxyable to begin with.

## DMARC policy

The default policy is `p=quarantine` with aggregate reports sent to `dmarc@<domain>`. To tighten to `p=reject`, edit the record manually in Cloudflare after provisioning — `ign8mail` will not overwrite it on re-runs unless you delete it first.

## Aggregate report ingestion

DMARC report mail arrives at `dmarc@<domain>`. Read it with:

```bash
ign8mail dmarc-stats
ign8mail dmarc-stats --delete   # consume after parsing
```

## What `ign8mail` does **not** create

- **PTR (reverse DNS)** — Hetzner sets PTR on the VM, but you need to set it to `mail.<domain>` via the Hetzner Cloud console. Without it, big mail providers will downgrade your reputation.
- **MTA-STS / TLSRPT** — not provisioned.
- **BIMI** — not provisioned.

## Adding webmail DNS

`ign8mail deploy-webmail` adds an additional record:

| Type | Name | Content | Proxied |
|------|------|---------|---------|
| **A** or **CNAME** | `webmail.<domain>` | server IPv4 | yes (Cloudflare-proxied) |
