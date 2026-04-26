"""RSA keypair generation for SSH access and DKIM signing."""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_rsa_keypair(bits: int = 4096) -> tuple[str, str]:
    """Return (private_key_pem, public_key_openssh)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return private_pem, public_openssh


def dkim_public_key_base64(public_key_pem: str) -> str:
    """Extract the base64-encoded modulus suitable for a DKIM TXT record."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    import base64

    pub = load_pem_public_key(public_key_pem.encode())
    der = pub.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode()


def dkim_private_key_pem(priv_path: Path) -> bytes:
    """Read an OpenSSH private key and return it as a PEM RSA private key (for rspamd)."""
    key = serialization.load_ssh_private_key(priv_path.read_bytes(), password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def save_keypair(directory: Path, name: str = "ign8mail") -> tuple[Path, Path]:
    """Generate a keypair if it doesn't exist, return (private_path, public_path)."""
    directory.mkdir(parents=True, exist_ok=True)
    priv_path = directory / name
    pub_path = directory / f"{name}.pub"

    if not priv_path.exists():
        private_pem, public_openssh = generate_rsa_keypair()
        priv_path.write_text(private_pem)
        priv_path.chmod(0o600)
        pub_path.write_text(public_openssh)
    return priv_path, pub_path
