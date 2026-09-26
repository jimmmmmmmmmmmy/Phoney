"""Load local configuration without exposing credentials in HTTP responses."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    account_sid: str
    auth_token: str = field(repr=False)
    public_base_url: str

    def __post_init__(self):
        if not self.account_sid.startswith("AC") or len(self.account_sid) != 34:
            raise ValueError("Set TWILIO_ACCOUNT_SID in .env to your account SID.")
        if not self.auth_token or self.auth_token == "REPLACE_ME":
            raise ValueError("Set TWILIO_AUTH_TOKEN in .env; API secrets cannot validate webhooks.")
        url = urlsplit(self.public_base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment):
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin. Run scripts/dev.py start.")

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            account_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        )
