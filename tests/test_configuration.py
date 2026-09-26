"""Configuration mistakes must fail before an outbound call can be reserved."""

import pytest

from config import Settings


BASE = dict(account_sid="AC" + "1" * 32, auth_token="test-auth",
            public_base_url="https://operator.example")


@pytest.mark.parametrize("fields", [
    {"callee_number": "+15555550100"},
    {"twilio_number": "+15555550100", "callee_number": "+15555550100"},
    {"callee_number": "3125550100", "twilio_number": "+15555550101"},
    {"twilio_number": "not-a-phone"},
    {"api_key": "SK" + "2" * 32},
    {"api_secret": "secret"},
    {"api_key": "invalid", "api_secret": "secret"},
    {"switchboard_setup_timeout": 0},
    {"switchboard_setup_timeout": 121},
    {"deploy_control_token": "short"},
])
def test_invalid_switchboard_configuration_fails(fields):
    with pytest.raises(ValueError):
        Settings(**BASE, **fields)


def test_configuration_keeps_sensitive_values_out_of_repr():
    settings = Settings(**BASE, twilio_number="+15555550100", callee_number="+15555550101",
                        api_key="SK" + "2" * 32, api_secret="private-rest-secret",
                        deploy_control_token="private-deploy-token-" * 3)
    assert settings.switchboard_ready
    for field in (settings.auth_token, settings.api_key, settings.api_secret,
                  settings.twilio_number, settings.callee_number, settings.deploy_control_token):
        assert field not in repr(settings)
