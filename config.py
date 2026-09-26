"""Load local configuration without exposing credentials in HTTP responses."""

import os
import re
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
    github_webhook_secret: str = field(default="", repr=False)
    deploy_repository: str = "jimmmmmmmmmmmy/fictional-rotary-phone"
    deploy_trigger_path: str = ""
    deploy_commit: str = ""
    twilio_number: str = field(default="", repr=False)
    callee_number: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    switchboard_setup_timeout: float = 45.0
    deploy_control_token: str = field(default="", repr=False)

    def __post_init__(self):
        if not self.account_sid.startswith("AC") or len(self.account_sid) != 34:
            raise ValueError("Set TWILIO_ACCOUNT_SID in .env to your account SID.")
        if not self.auth_token or self.auth_token == "REPLACE_ME":
            raise ValueError("Set TWILIO_AUTH_TOKEN in .env; API secrets cannot validate webhooks.")
        url = urlsplit(self.public_base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment):
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin. Run scripts/dev.py start.")
        if self.deploy_trigger_path and not Path(self.deploy_trigger_path).is_absolute():
            raise ValueError("DEPLOY_TRIGGER_PATH must be an absolute path.")
        for name, number in (("TWILIO_NUMBER", self.twilio_number),
                             ("CALLEE_NUMBER", self.callee_number)):
            if number and not re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
                raise ValueError(f"{name} must be an E.164 phone number including country code.")
        if self.callee_number and not self.twilio_number:
            raise ValueError("Set TWILIO_NUMBER before enabling CALLEE_NUMBER.")
        if self.callee_number and self.callee_number == self.twilio_number:
            raise ValueError("CALLEE_NUMBER must differ from TWILIO_NUMBER.")
        if bool(self.api_key) != bool(self.api_secret):
            raise ValueError("Set TWILIO_API_KEY and TWILIO_API_SECRET together, or neither.")
        if self.api_key and not re.fullmatch(r"SK[0-9a-fA-F]{32}", self.api_key):
            raise ValueError("TWILIO_API_KEY must be an API key SID.")
        if not 1 <= self.switchboard_setup_timeout <= 120:
            raise ValueError("Switchboard setup timeout must be between 1 and 120 seconds.")
        if self.deploy_control_token and len(self.deploy_control_token) < 32:
            raise ValueError("DEPLOY_CONTROL_TOKEN must contain at least 32 characters.")

    @property
    def switchboard_ready(self):
        return bool(self.callee_number and self.twilio_number)

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            account_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET", "").strip(),
            deploy_repository=os.getenv(
                "DEPLOY_REPOSITORY", "jimmmmmmmmmmmy/fictional-rotary-phone").strip(),
            deploy_trigger_path=os.getenv("DEPLOY_TRIGGER_PATH", "").strip(),
            deploy_commit=os.getenv("DEPLOY_COMMIT", "").strip(),
            twilio_number=os.getenv("TWILIO_NUMBER", "").strip(),
            callee_number=os.getenv("CALLEE_NUMBER", "").strip(),
            api_key=os.getenv("TWILIO_API_KEY", "").strip(),
            api_secret=os.getenv("TWILIO_API_SECRET", "").strip(),
            deploy_control_token=os.getenv("DEPLOY_CONTROL_TOKEN", "").strip(),
        )
