import hashlib
import hmac
import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import MAX_GITHUB_BODY_BYTES, create_app
from config import Settings

SECRET = "github-test-secret"
REPOSITORY = "jimmmmmmmmmmmy/fictional-rotary-phone"
PUSH = {"ref": "refs/heads/main", "repository": {"full_name": REPOSITORY},
        "deleted": False, "after": "untrusted-payload-sha"}


@pytest.fixture
def settings(tmp_path):
    return Settings("AC" + "1" * 32, "test-secret", "https://operator.example",
                    github_webhook_secret=SECRET,
                    deploy_trigger_path=str(tmp_path / "deploy.trigger"))


def signed_post(client, payload=None, *, body=None, event="push", secret=SECRET):
    if body is None:
        body = json.dumps(PUSH if payload is None else payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/github/webhook", content=body,
                       headers={"Content-Type": "application/json", "X-GitHub-Event": event,
                                "X-Hub-Signature-256": signature,
                                "X-GitHub-Delivery": "test-delivery"})


def test_signed_main_push_atomically_replaces_private_trigger(settings):
    path = Path(settings.deploy_trigger_path)
    path.write_text("old marker")
    path.chmod(0o644)
    old_inode = path.stat().st_ino
    response = signed_post(TestClient(create_app(settings)))
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    marker = json.loads(path.read_text())
    assert set(marker) == {"received_at_ns", "delivery_id"}
    assert marker["received_at_ns"] > 0
    assert marker["delivery_id"] == "test-delivery"
    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_signed_push_uses_renamed_repository_without_losing_existing_config(settings):
    payload = {**PUSH, "repository": {"full_name": "jimmmmmmmmmmmy/Phoney"}}
    response = signed_post(TestClient(create_app(settings)), payload)
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert Path(settings.deploy_trigger_path).exists()


@pytest.mark.parametrize("signature", [None, "sha256=wrong", "sha1=wrong"])
def test_unsigned_and_invalid_signature_do_not_write(settings, signature):
    headers = {"X-GitHub-Event": "push"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    response = TestClient(create_app(settings)).post("/github/webhook", json=PUSH, headers=headers)
    assert response.status_code == 403
    assert not Path(settings.deploy_trigger_path).exists()


def test_tampered_body_is_rejected(settings):
    original = json.dumps(PUSH).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), original, hashlib.sha256).hexdigest()
    response = TestClient(create_app(settings)).post(
        "/github/webhook", content=original + b" ",
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": signature})
    assert response.status_code == 403
    assert not Path(settings.deploy_trigger_path).exists()


@pytest.mark.parametrize(("payload", "event", "reason"), [
    ({**PUSH, "repository": {"full_name": "someone/another-repo"}}, "push", "repository"),
    ({**PUSH, "ref": "refs/heads/feature"}, "push", "branch"),
    ({**PUSH, "ref": "refs/tags/main"}, "push", "branch"),
    ({**PUSH, "deleted": True}, "push", "deleted"),
    (PUSH, "pull_request", "event"),
])
def test_unrelated_events_do_not_write(settings, payload, event, reason):
    response = signed_post(TestClient(create_app(settings)), payload, event=event)
    assert response.status_code == 202
    assert response.json() == {"status": "ignored", "reason": reason}
    assert not Path(settings.deploy_trigger_path).exists()


def test_ping_acknowledged_without_trigger(settings):
    response = signed_post(TestClient(create_app(settings)), {"zen": "Hello"}, event="ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "event": "ping"}
    assert not Path(settings.deploy_trigger_path).exists()


@pytest.mark.parametrize("body", [b"{broken", b"[]", b"null", b"\xff"])
def test_malformed_payload_rejected(settings, body):
    assert signed_post(TestClient(create_app(settings)), body=body).status_code == 400
    assert not Path(settings.deploy_trigger_path).exists()


def test_oversized_body_rejected(settings):
    response = signed_post(TestClient(create_app(settings)), body=b"x" * (MAX_GITHUB_BODY_BYTES + 1))
    assert response.status_code == 413
    assert not Path(settings.deploy_trigger_path).exists()


@pytest.mark.parametrize("missing", ["github_webhook_secret", "deploy_trigger_path", "deploy_repository"])
def test_missing_configuration_fails_closed(settings, missing):
    client = TestClient(create_app(replace(settings, **{missing: ""})))
    assert signed_post(client).status_code == 503


def test_unwritable_trigger_fails_closed(settings, tmp_path):
    client = TestClient(create_app(replace(settings, deploy_trigger_path=str(tmp_path / "missing" / "trigger"))))
    assert signed_post(client).status_code == 503


def test_trigger_path_must_be_absolute(settings):
    with pytest.raises(ValueError, match="absolute"):
        replace(settings, deploy_trigger_path="relative/trigger")


def test_health_reports_runtime_commit_only_when_configured(settings):
    assert "commit" not in TestClient(create_app(settings)).get("/health").json()
    client = TestClient(create_app(replace(settings, deploy_commit="abc123")))
    assert client.get("/health").json()["commit"] == "abc123"
