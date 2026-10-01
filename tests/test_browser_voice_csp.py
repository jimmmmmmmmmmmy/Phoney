"""The enabled browser SDK can signal/play audio without loosening other pages."""

from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from app import create_app
from config import Settings
from dashboard import html_page


BASE = Settings("AC" + "1" * 32, "offline-auth-token", "https://operator.example")


def directives(response):
    return {parts[0]: set(parts[1:]) for directive in response.headers["content-security-policy"].split(";")
            if (parts := directive.strip().split())}


@pytest.mark.parametrize("enabled", [False, True])
def test_dashboard_allows_only_required_default_sdk_connections_when_enabled(enabled):
    settings = replace(BASE, browser_voice_enabled=enabled, api_key="SK" + "2" * 32,
                       api_secret="offline-api-secret-" * 3, twilio_browser_app_sid="AP" + "3" * 32,
                       twilio_conference_app_sid="AP" + "4" * 32)
    with TestClient(create_app(settings), base_url=settings.public_base_url) as client:
        response = client.get("/dashboard")
    assert response.status_code == 200
    policy = directives(response)
    expected_connect = {"'self'", "wss://operator.example"}
    expected_media = {"'self'"}
    if enabled:
        expected_connect |= {"https://eventgw.twilio.com", "wss://voice-js.roaming.twilio.com",
                             "https://media.twiliocdn.com", "https://sdk.twilio.com"}
        expected_media |= {"mediastream:", "blob:", "https://media.twiliocdn.com", "https://sdk.twilio.com"}
    assert policy["connect-src"] == expected_connect
    assert policy["media-src"] == expected_media
    assert policy["default-src"] == policy["frame-ancestors"] == policy["base-uri"] == {"'none'"}
    assert policy["form-action"] == {"'self'"}
    assert policy["script-src"] & {"'unsafe-inline'", "'unsafe-eval'", "*", "blob:"} == set()
    assert all(source == "'self'" or source.startswith("'sha256-") for source in policy["script-src"])
    assert response.headers["permissions-policy"] == "microphone=(self)"


@pytest.mark.parametrize("filename", ["pin_unlock.html"])
def test_non_dashboard_pages_never_inherit_sdk_permissions_even_if_flag_passed(filename):
    policy = directives(html_page(filename, browser_voice_enabled=True))
    assert policy["connect-src"] == {"'self'"}
    assert policy["media-src"] == {"'self'"}
    assert "twilio" not in str(policy)


def test_default_html_renderer_keeps_dashboard_sdk_connections_disabled():
    policy = directives(html_page("dashboard.html"))
    assert policy["connect-src"] == policy["media-src"] == {"'self'"}
