"""Small, single-process call state; no phone numbers or credentials are retained."""

from dataclasses import dataclass, field
import time


class SessionRejected(Exception):
    """The switchboard cannot accept a new caller."""


@dataclass
class CallSession:
    parent_sid: str
    conference_name: str
    phase: str = "waiting"
    conference_sid: str = ""
    outbound_sid: str = ""
    extra_outbound_sid: str = ""
    connected: bool = False
    callee_answered: bool = False
    reason: str = ""
    caller_joined: bool = False
    callee_joined: bool = False
    conference_started: bool = False
    dial_reserved: bool = False
    created_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    last_event_sequence: int = -1
    call_status: str = ""
    seen_events: set[tuple[str, str, str]] = field(default_factory=set, repr=False)
    pending_status: dict[str, str] = field(default_factory=dict, repr=False)
    pending_answered: set[str] = field(default_factory=set, repr=False)
    voicemail_confirmed: bool = False
    voicemail_caller_left: bool = False
    voicemail_room_cleaned: bool = False
    voicemail_reason: str = ""
