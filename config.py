"""Load local configuration without exposing credentials in HTTP responses."""

import os
import re
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    media_capture_enabled: bool = False
    media_storage_dir: str = ""
    media_max_seconds: int = 1800
    transcription_enabled: bool = False
    deepgram_api_key: str = field(default="", repr=False)
    deepgram_model: str = "nova-3"
    transcript_storage_dir: str = ""
    voicemail_enabled: bool = False
    voicemail_max_seconds: int = 120
    voicemail_storage_dir: str = ""
    call_details_storage_dir: str = ""
    workspace_storage_dir: str = ""
    database_url: str = field(default="", repr=False)
    workspace_id: str = "default"
    workspace_access_enabled: bool = False
    gemini_api_key: str = field(default="", repr=False)
    gemini_summary_model: str = "gemini-3.8-flash"
    owner_number: str = field(default="", repr=False)
    allowed_destinations: tuple[str, ...] = ()
    allowed_destination_countries: tuple[str, ...] = ()
    public_calling_enabled: bool = False
    operator_admin_token: str = field(default="", repr=False)
    max_call_seconds: int = 1800
    voice_agent_enabled: bool = False
    agent_management_enabled: bool = False
    agent_demo_mode: bool = False
    operator_inbound_enabled: bool = False
    automatic_takeover_enabled: bool = False
    voicemail_agent_enabled: bool = False
    voicemail_agent_ring_seconds: int = 10
    modulate_detection_enabled: bool = False
    modulate_backfill_enabled: bool = False
    modulate_api_key: str = field(default="", repr=False)
    detection_storage_dir: str = ""
    modulate_detection_max_audio_seconds: int = 120
    modulate_detection_deadline_seconds: float = 45.0
    modulate_detection_min_confidence: float = 0.80
    modulate_detection_queue_frames: int = 250

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
        if type(self.media_capture_enabled) is not bool:
            raise ValueError("MEDIA_CAPTURE_ENABLED must be true or false.")
        if self.media_capture_enabled and not Path(self.media_storage_dir).is_absolute():
            raise ValueError("Set MEDIA_STORAGE_DIR to an absolute private directory before enabling capture.")
        if type(self.media_max_seconds) is not int or not 1 <= self.media_max_seconds <= 3600:
            raise ValueError("MEDIA_MAX_SECONDS must be between 1 and 3600 seconds.")
        if type(self.transcription_enabled) is not bool:
            raise ValueError("TRANSCRIPTION_ENABLED must be true or false.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", self.deepgram_model):
            raise ValueError("DEEPGRAM_MODEL must be a model identifier.")
        if self.transcription_enabled:
            if not self.media_capture_enabled:
                raise ValueError("Enable MEDIA_CAPTURE_ENABLED before live transcription.")
            if not self.deepgram_api_key or self.deepgram_api_key == "REPLACE_ME":
                raise ValueError("Set DEEPGRAM_API_KEY before enabling transcription.")
            if not Path(self.transcript_storage_dir).is_absolute():
                raise ValueError("TRANSCRIPT_STORAGE_DIR must be an absolute private directory.")
        if type(self.voicemail_enabled) is not bool:
            raise ValueError("VOICEMAIL_ENABLED must be true or false.")
        if type(self.voicemail_max_seconds) is not int or not 2 <= self.voicemail_max_seconds <= 600:
            raise ValueError("VOICEMAIL_MAX_SECONDS must be between 2 and 600 seconds.")
        if self.voicemail_enabled and not Path(self.voicemail_storage_dir).is_absolute():
            raise ValueError("VOICEMAIL_STORAGE_DIR must be an absolute private directory.")
        if self.call_details_storage_dir and not Path(self.call_details_storage_dir).is_absolute():
            raise ValueError("CALL_DETAILS_STORAGE_DIR must be an absolute private directory.")
        if self.workspace_storage_dir:
            workspace_path = Path(self.workspace_storage_dir)
            if (not workspace_path.is_absolute() or workspace_path == Path(workspace_path.anchor)
                    or ".." in workspace_path.parts):
                raise ValueError("WORKSPACE_STORAGE_DIR must be an absolute private directory.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.workspace_id):
            raise ValueError("WORKSPACE_ID must be a lowercase identifier of at most 64 characters.")
        if type(self.workspace_access_enabled) is not bool:
            raise ValueError("WORKSPACE_ACCESS_ENABLED must be true or false.")
        if self.database_url:
            try:
                database = urlsplit(self.database_url)
                if (database.scheme not in {"postgres", "postgresql"}
                        or database.hostname not in {None, "localhost", "127.0.0.1", "::1"}
                        or not database.path.strip("/") or database.fragment):
                    raise ValueError
                database.port
                options = parse_qs(database.query)
                if "service" in options:
                    raise ValueError
                for option in ("host", "hostaddr"):
                    for host in options.get(option, []):
                        if host not in {"localhost", "127.0.0.1", "::1"} and not (
                                option == "host" and host.startswith("/") and "," not in host):
                            raise ValueError
            except ValueError:
                raise ValueError("DATABASE_URL must identify a local PostgreSQL database.") from None
        if self.workspace_access_enabled and not (self.database_url or self.workspace_storage_dir):
            raise ValueError("Set DATABASE_URL or WORKSPACE_STORAGE_DIR before enabling workspace access.")
        if self.call_details_storage_dir:
            details_path = Path(self.call_details_storage_dir).resolve()
            if any(path and Path(path).resolve() == details_path
                   for path in (self.transcript_storage_dir, self.voicemail_storage_dir)):
                raise ValueError("CALL_DETAILS_STORAGE_DIR must differ from transcript and voicemail storage.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,99}", self.gemini_summary_model):
            raise ValueError("GEMINI_SUMMARY_MODEL must be a model identifier without a path.")
        if self.owner_number and not re.fullmatch(r"\+[1-9][0-9]{7,14}", self.owner_number):
            raise ValueError("OWNER_NUMBER must be an E.164 phone number including country code.")
        if len(set(self.allowed_destinations)) != len(self.allowed_destinations):
            raise ValueError("ALLOWED_DESTINATIONS lists the same number more than once.")
        for destination in self.allowed_destinations:
            if not re.fullmatch(r"\+[1-9][0-9]{7,14}", destination):
                raise ValueError("ALLOWED_DESTINATIONS values must be E.164 phone numbers.")
            # Loops are prevented at configuration time, not only per request.
            if destination in {self.owner_number, self.twilio_number}:
                raise ValueError("ALLOWED_DESTINATIONS cannot contain OWNER_NUMBER or TWILIO_NUMBER.")
        if (not isinstance(self.allowed_destination_countries, tuple)
                or any(country != "US" for country in self.allowed_destination_countries)
                or len(set(self.allowed_destination_countries)) != len(self.allowed_destination_countries)):
            raise ValueError("ALLOWED_DESTINATION_COUNTRIES supports US only, listed once, or an empty value.")
        if self.operator_admin_token and len(self.operator_admin_token) < 32:
            raise ValueError("OPERATOR_ADMIN_TOKEN must contain at least 32 characters.")
        if type(self.public_calling_enabled) is not bool:
            raise ValueError("PUBLIC_CALLING_ENABLED must be true or false.")
        if type(self.max_call_seconds) is not int or not 30 <= self.max_call_seconds <= 14400:
            raise ValueError("MAX_CALL_SECONDS must be between 30 and 14400 seconds.")
        if type(self.voice_agent_enabled) is not bool:
            raise ValueError("VOICE_AGENT_ENABLED must be true or false.")
        if type(self.agent_management_enabled) is not bool:
            raise ValueError("AGENT_MANAGEMENT_ENABLED must be true or false.")
        if type(self.agent_demo_mode) is not bool:
            raise ValueError("AGENT_DEMO_MODE must be true or false.")
        if self.agent_demo_mode and not self.agent_management_enabled:
            raise ValueError("Enable AGENT_MANAGEMENT_ENABLED before AGENT_DEMO_MODE.")
        if type(self.operator_inbound_enabled) is not bool:
            raise ValueError("OPERATOR_INBOUND_ENABLED must be true or false.")
        if self.agent_management_enabled and not (self.workspace_storage_dir or self.database_url):
            raise ValueError("Set WORKSPACE_STORAGE_DIR or DATABASE_URL before enabling agent management.")
        if self.operator_inbound_enabled:
            if not self.operator_ready or not self.voice_agent_enabled:
                raise ValueError("Configure the operator bridge and VOICE_AGENT_ENABLED before inbound routing.")
            if not self.agent_management_enabled or not self.transcription_enabled:
                raise ValueError("Inbound operator calls require agent management and transcription.")
        for name, flag in (("AUTOMATIC_TAKEOVER_ENABLED", self.automatic_takeover_enabled),
                           ("VOICEMAIL_AGENT_ENABLED", self.voicemail_agent_enabled)):
            if type(flag) is not bool:
                raise ValueError(f"{name} must be true or false.")
            if flag and not self.operator_inbound_enabled:
                raise ValueError(f"Enable OPERATOR_INBOUND_ENABLED before {name}.")
        if self.automatic_takeover_enabled and not self.modulate_detection_enabled:
            raise ValueError("Automatic takeover requires live Modulate detection.")
        if (type(self.voicemail_agent_ring_seconds) is not int
                or not 5 <= self.voicemail_agent_ring_seconds <= 60):
            raise ValueError("VOICEMAIL_AGENT_RING_SECONDS must be between 5 and 60.")
        if type(self.modulate_detection_enabled) is not bool:
            raise ValueError("MODULATE_DETECTION_ENABLED must be true or false.")
        if type(self.modulate_backfill_enabled) is not bool:
            raise ValueError("MODULATE_BACKFILL_ENABLED must be true or false.")
        if self.modulate_backfill_enabled and not self.modulate_detection_enabled:
            raise ValueError("Enable MODULATE_DETECTION_ENABLED before recorded caller analysis.")
        if (type(self.modulate_detection_max_audio_seconds) is not int
                or not 4 <= self.modulate_detection_max_audio_seconds <= 120):
            raise ValueError("MODULATE_DETECTION_MAX_AUDIO_SECONDS must be between 4 and 120.")
        if (type(self.modulate_detection_queue_frames) is not int
                or not 1 <= self.modulate_detection_queue_frames <= 1000):
            raise ValueError("MODULATE_DETECTION_QUEUE_FRAMES must be between 1 and 1000.")
        for name, value, low, high in (
            ("MODULATE_DETECTION_DEADLINE_SECONDS", self.modulate_detection_deadline_seconds, 1, 120),
            ("MODULATE_DETECTION_MIN_CONFIDENCE", self.modulate_detection_min_confidence, 0.5, 1),
        ):
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not low <= value <= high):
                raise ValueError(f"{name} must be between {low} and {high}.")
        if self.detection_storage_dir:
            detection_path = Path(self.detection_storage_dir)
            if (not detection_path.is_absolute() or detection_path == Path(detection_path.anchor)
                    or ".." in detection_path.parts):
                raise ValueError("DETECTION_STORAGE_DIR must be an absolute private directory.")
            # Keep advisory metadata separate so rollback cannot invalidate call records.
            if any(path and Path(path).resolve() == detection_path.resolve()
                   for path in (self.media_storage_dir, self.transcript_storage_dir,
                                self.voicemail_storage_dir, self.call_details_storage_dir)):
                raise ValueError("DETECTION_STORAGE_DIR must differ from other storage directories.")
        if self.modulate_detection_enabled:
            if not self.media_capture_enabled:
                raise ValueError("Enable MEDIA_CAPTURE_ENABLED before live detection.")
            if not self.modulate_api_key or self.modulate_api_key == "REPLACE_ME":
                raise ValueError("Set MODULATE_API_KEY before enabling detection.")
            if not self.detection_storage_dir:
                raise ValueError("Set DETECTION_STORAGE_DIR before enabling detection.")

    @property
    def switchboard_ready(self):
        return bool(self.callee_number and self.twilio_number)

    @property
    def operator_ready(self):
        """Whether an explicitly configured bridge can reserve call sessions.

        Outbound destinations require an explicit number or country policy;
        inbound routing is independently opted in. Keep this in step with
        OperatorSessions.ready.
        """
        return bool(self.owner_number and self.twilio_number
                    and self.operator_admin_token
                    and (self.allowed_destinations or self.allowed_destination_countries
                         or self.operator_inbound_enabled))

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        public_calling_flag = os.getenv("PUBLIC_CALLING_ENABLED", "false").strip().lower()
        if public_calling_flag not in {"true", "false"}:
            raise ValueError("PUBLIC_CALLING_ENABLED must be true or false.")
        capture_flag = os.getenv("MEDIA_CAPTURE_ENABLED", "false").strip().lower()
        if capture_flag not in {"true", "false"}:
            raise ValueError("MEDIA_CAPTURE_ENABLED must be true or false.")
        transcription_flag = os.getenv("TRANSCRIPTION_ENABLED", "false").strip().lower()
        if transcription_flag not in {"true", "false"}:
            raise ValueError("TRANSCRIPTION_ENABLED must be true or false.")
        voicemail_flag = os.getenv("VOICEMAIL_ENABLED", "false").strip().lower()
        if voicemail_flag not in {"true", "false"}:
            raise ValueError("VOICEMAIL_ENABLED must be true or false.")
        voice_flag = os.getenv("VOICE_AGENT_ENABLED", "false").strip().lower()
        if voice_flag not in {"true", "false"}:
            raise ValueError("VOICE_AGENT_ENABLED must be true or false.")
        management_flag = os.getenv("AGENT_MANAGEMENT_ENABLED", "false").strip().lower()
        if management_flag not in {"true", "false"}:
            raise ValueError("AGENT_MANAGEMENT_ENABLED must be true or false.")
        demo_flag = os.getenv("AGENT_DEMO_MODE", "false").strip().lower()
        if demo_flag not in {"true", "false"}:
            raise ValueError("AGENT_DEMO_MODE must be true or false.")
        access_flag = os.getenv("WORKSPACE_ACCESS_ENABLED", "false").strip().lower()
        if access_flag not in {"true", "false"}:
            raise ValueError("WORKSPACE_ACCESS_ENABLED must be true or false.")
        inbound_flag = os.getenv("OPERATOR_INBOUND_ENABLED", "false").strip().lower()
        if inbound_flag not in {"true", "false"}:
            raise ValueError("OPERATOR_INBOUND_ENABLED must be true or false.")
        auto_flag = os.getenv("AUTOMATIC_TAKEOVER_ENABLED", "false").strip().lower()
        voicemail_agent_flag = os.getenv("VOICEMAIL_AGENT_ENABLED", "false").strip().lower()
        if auto_flag not in {"true", "false"}:
            raise ValueError("AUTOMATIC_TAKEOVER_ENABLED must be true or false.")
        if voicemail_agent_flag not in {"true", "false"}:
            raise ValueError("VOICEMAIL_AGENT_ENABLED must be true or false.")
        detection_flag = os.getenv("MODULATE_DETECTION_ENABLED", "false").strip().lower()
        if detection_flag not in {"true", "false"}:
            raise ValueError("MODULATE_DETECTION_ENABLED must be true or false.")
        backfill_flag = os.getenv("MODULATE_BACKFILL_ENABLED", "false").strip().lower()
        if backfill_flag not in {"true", "false"}:
            raise ValueError("MODULATE_BACKFILL_ENABLED must be true or false.")
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
            media_capture_enabled=capture_flag == "true",
            media_storage_dir=os.getenv("MEDIA_STORAGE_DIR", "").strip(),
            media_max_seconds=int(os.getenv("MEDIA_MAX_SECONDS", "1800")),
            transcription_enabled=transcription_flag == "true",
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", "").strip(),
            deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-3").strip(),
            transcript_storage_dir=os.getenv("TRANSCRIPT_STORAGE_DIR", "").strip(),
            voicemail_enabled=voicemail_flag == "true",
            voicemail_max_seconds=int(os.getenv("VOICEMAIL_MAX_SECONDS", "120")),
            voicemail_storage_dir=os.getenv("VOICEMAIL_STORAGE_DIR", "").strip(),
            call_details_storage_dir=os.getenv("CALL_DETAILS_STORAGE_DIR", "").strip(),
            workspace_storage_dir=os.getenv("WORKSPACE_STORAGE_DIR", "").strip(),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            workspace_id=os.getenv("WORKSPACE_ID", "default").strip(),
            workspace_access_enabled=access_flag == "true",
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_summary_model=os.getenv("GEMINI_SUMMARY_MODEL", "gemini-3.8-flash").strip(),
            owner_number=os.getenv("OWNER_NUMBER", "").strip(),
            allowed_destinations=tuple(
                value.strip() for value in os.getenv("ALLOWED_DESTINATIONS", "").split(",")
                if value.strip()),
            allowed_destination_countries=tuple(
                value.strip().upper() for value in os.getenv("ALLOWED_DESTINATION_COUNTRIES", "").split(",")
                if value.strip()),
            operator_admin_token=os.getenv("OPERATOR_ADMIN_TOKEN", "").strip(),
            public_calling_enabled=public_calling_flag == "true",
            max_call_seconds=int(os.getenv("MAX_CALL_SECONDS", "1800")),
            voice_agent_enabled=voice_flag == "true",
            agent_management_enabled=management_flag == "true",
            agent_demo_mode=demo_flag == "true",
            operator_inbound_enabled=inbound_flag == "true",
            automatic_takeover_enabled=auto_flag == "true",
            voicemail_agent_enabled=voicemail_agent_flag == "true",
            voicemail_agent_ring_seconds=int(os.getenv("VOICEMAIL_AGENT_RING_SECONDS", "10")),
            modulate_detection_enabled=detection_flag == "true",
            modulate_backfill_enabled=backfill_flag == "true",
            modulate_api_key=os.getenv("MODULATE_API_KEY", "").strip(),
            detection_storage_dir=os.getenv("DETECTION_STORAGE_DIR", "").strip(),
            modulate_detection_max_audio_seconds=int(os.getenv("MODULATE_DETECTION_MAX_AUDIO_SECONDS", "120")),
            modulate_detection_deadline_seconds=float(os.getenv("MODULATE_DETECTION_DEADLINE_SECONDS", "45")),
            modulate_detection_min_confidence=float(os.getenv("MODULATE_DETECTION_MIN_CONFIDENCE", "0.80")),
            modulate_detection_queue_frames=int(os.getenv("MODULATE_DETECTION_QUEUE_FRAMES", "250")),
        )
