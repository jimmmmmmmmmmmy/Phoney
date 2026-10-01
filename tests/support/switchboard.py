"""Shared fixtures and fakes for focused integration checks."""

from config import Settings


SETTINGS = Settings(
    account_sid="AC" + "1" * 32,
    auth_token="switchboard-test-secret",
    public_base_url="https://operator.example",
    twilio_number="+15555550200",
    callee_number="+15555550100",
)
