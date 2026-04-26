from pydantic import EmailStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IGN8_", env_file=".env", extra="ignore")

    # Required
    domain: str
    admin_email: EmailStr
    hetzner_token: str
    cloudflare_token: str
    cloudflare_zone_id: str
    cloudflare_email: str = ""   # required when using a Global API Key; omit for API Tokens

    # Server
    server_location: str = "nbg1"      # Hetzner datacenter (nbg1, fsn1, hel1)
    server_type: str = "cx23"          # Hetzner server type
    server_image: str = "ubuntu-24.04"

    # Mail
    mailbox_quota_mb: int = 2048
    max_message_size_mb: int = 50

    @field_validator("domain")
    @classmethod
    def strip_trailing_dot(cls, v: str) -> str:
        return v.rstrip(".")
