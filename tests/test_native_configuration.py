"""Native pilot settings and signed-service gate never broaden public access."""
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest
from twilio.request_validator import RequestValidator

from app import create_app
from config import Settings
from workspace_auth.web import TWILIO_HTTP, TWILIO_WS


BASE = Settings("AC" + "1" * 32, "offline-auth-token", "https://operator.example")
SESSION = "a" * 32


def test_native_configuration_defaults_off_and_requires_a_valid_app():
    assert not BASE.native_conference_enabled
    for fields in ({"native_conference_enabled": "true"},
                   {"native_conference_enabled": True},
                   {"twilio_conference_app_sid": "APbad"}):
        with pytest.raises(ValueError):
            replace(BASE, **fields)
    assert replace(BASE, native_conference_enabled=True,
                   twilio_conference_app_sid="AP" + "2" * 32).native_conference_enabled


@pytest.mark.parametrize("path", [
    "/twilio/native-agent", f"/twilio/native-agent/status/{SESSION}/1",
    f"/twilio/native-owner/{SESSION}", f"/twilio/native-menu/{SESSION}",
    f"/twilio/native-command/{SESSION}", f"/twilio/native-conference/{SESSION}",
    f"/twilio/native-finished/{SESSION}/remote",
])
def test_native_http_routes_require_signed_service_access(tmp_path, path):
    assert TWILIO_HTTP.fullmatch(path)
    settings = replace(BASE, workspace_access_enabled=True, workspace_storage_dir=str(tmp_path))
    with TestClient(create_app(settings), base_url=BASE.public_base_url) as client:
        form = {"AccountSid": BASE.account_sid, "CallSid": "CA" + "3" * 32}
        assert client.post(path, data=form).status_code == 403
        signature = RequestValidator(BASE.auth_token).compute_signature(BASE.public_base_url + path, form)
        response = client.post(path, data=form, headers={"X-Twilio-Signature": signature})
        # Signed requests reach the endpoint even without a workspace cookie;
        # unknown sessions are rejected there. The app endpoint hangs up safely.
        assert response.status_code == (200 if path == "/twilio/native-agent" else 400)


@pytest.mark.parametrize("path", [f"/conference-media/{SESSION}/owner/",
                                  f"/conference-media/{SESSION}/remote/",
                                  f"/native-agent-media/{SESSION}/"])
def test_only_exact_native_socket_paths_are_service_exempt(path):
    assert TWILIO_WS.fullmatch(path)
    assert not TWILIO_WS.fullmatch(path[:-1])
    assert not TWILIO_WS.fullmatch(path.replace(SESSION, "invalid"))
    if path.startswith("/conference-media/"):
        assert not TWILIO_WS.fullmatch(path.replace("owner", "visitor").replace("remote", "visitor"))
