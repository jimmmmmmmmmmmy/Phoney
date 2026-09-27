"""Owner session, publishing, catalog and clone boundaries (no live providers)."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from agent_registry import AgentRegistry, OWNER_COOKIE, register_agent_routes
from agent_registry.routes import MAX_JSON, MAX_UPLOAD, MAX_SAMPLE

AGENT = "agent-12345678-1234-1234-1234-123456789abc"


def voice(**changes):
    return {"name": "Owner voice", "voiceId": "voiceABC123456789", "ready": True,
            "requiresVerification": False, **changes}


def config(**changes):
    return {"name": "Reception", "prompt": "Ask how we can help.",
            "voiceProfileId": "voice-voiceABC123456789", "slot": 1, **changes}

BASE = "https://dashboard.example"
HEADERS = {"Origin": BASE, "X-Agent-Request": "1"}
WAV = b"RIFF" + b"\x20\x00\x00\x00" + b"WAVE" + b"fake sample for mocked provider"


class Provider:
    def __init__(self):
        self.lists = 0
        self.clones = []

    async def list_voices(self):
        self.lists += 1
        return [voice()]

    async def clone(self, name, samples):
        self.clones.append((name, samples))
        return voice(name=name, ready=False, requiresVerification=True)


def client_for(tmp_path, *, enabled=True, provider=None, demo=False, automatic=False):
    app = FastAPI()
    registry = AgentRegistry(str(tmp_path))
    settings = SimpleNamespace(public_base_url=BASE, agent_management_enabled=enabled,
                               voice_agent_enabled=False, operator_inbound_enabled=False,
                               agent_demo_mode=demo, automatic_takeover_enabled=automatic)
    register_agent_routes(app, settings, registry, provider=provider)
    return TestClient(app, base_url=BASE), registry


def unlock(client, registry):
    return client.post("/api/agents/session", json={"code": registry.grant()}, headers=HEADERS)


def test_unauthed_config_reveals_no_agent_prompts_or_voice_ids(tmp_path):
    client, registry = client_for(tmp_path, provider=Provider())
    registry.add_voice(voice())
    registry.publish(AGENT, config())
    result = client.get("/api/agents/config")
    assert result.status_code == 200
    data = result.json()
    assert data["authenticated"] is False
    assert data["agents"] == data["voices"] == []
    assert data["automaticEnabled"] is data["manualEnabled"] is data["inboundEnabled"] is False
    assert "Ask how" not in result.text and "voiceABC" not in result.text


@pytest.mark.parametrize("automatic", [False, True])
def test_config_reports_automatic_handoff_flag_without_revealing_prompts(tmp_path, automatic):
    client, registry = client_for(tmp_path, automatic=automatic)
    registry.add_voice(voice())
    registry.publish(AGENT, config())
    result = client.get("/api/agents/config")
    assert result.status_code == 200
    assert result.json()["automaticEnabled"] is automatic
    assert result.json()["agents"] == result.json()["voices"] == []
    assert "Ask how" not in result.text and "voiceABC" not in result.text


def test_demo_can_read_and_save_agent_without_owner_session(tmp_path):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider, demo=True)
    registry.add_voice(voice())
    response = client.put(f"/api/agents/{AGENT}", json=config(expectedRevision=0), headers=HEADERS)
    assert response.status_code == 200
    data = client.get("/api/agents/config").json()
    assert data["demoMode"] is data["authenticated"] is True
    assert data["agents"] == [response.json()]
    assert len(data["voices"]) == 1
    assert data["automaticEnabled"] is False
    assert not client.cookies
    assert provider.lists == 0 and provider.clones == []
    restarted, _ = client_for(tmp_path, demo=True)
    assert restarted.get("/api/agents/config").json()["agents"] == data["agents"]


def test_demo_still_requires_same_origin_and_protects_provider_operations(tmp_path):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider, demo=True)
    registry.add_voice(voice())
    for headers in ({}, {"Origin": "https://other.example", "X-Agent-Request": "1"}):
        assert client.put(f"/api/agents/{AGENT}", json=config(), headers=headers).status_code == 403
    assert client.post("/api/agents/voices/refresh", json={}, headers=HEADERS).status_code == 403
    assert client.post("/api/agents/voices/clone", headers=HEADERS).status_code == 403
    assert provider.lists == 0 and provider.clones == []
    assert registry.snapshot()["agents"] == []


def test_single_use_unlock_cookie_flags_logout_and_restart(tmp_path):
    client, registry = client_for(tmp_path)
    code = registry.grant()
    response = client.post("/api/agents/session", json={"code": code}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["authenticated"]
    cookie = response.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "__Host-operator-owner="):
        assert flag in cookie
    assert "domain=" not in cookie.lower() and code not in cookie
    assert client.post("/api/agents/session", json={"code": code}, headers=HEADERS).status_code == 403
    assert client.get("/api/agents/config").json()["authenticated"]
    restarted, _ = client_for(tmp_path)
    restarted.cookies.update(client.cookies)
    assert restarted.get("/api/agents/config").json()["authenticated"]
    assert client.delete("/api/agents/session", headers=HEADERS).status_code == 200
    assert not restarted.get("/api/agents/config").json()["authenticated"]


@pytest.mark.parametrize("headers", [{}, {"Origin": BASE}, {"X-Agent-Request": "1"},
                                     {"Origin": "https://external.example", "X-Agent-Request": "1"},
                                     {"Origin": "null", "X-Agent-Request": "1"},
                                     {"Origin": "http://dashboard.example", "X-Agent-Request": "1"}])
def test_bootstrap_and_mutations_require_csrf_headers(tmp_path, headers):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider)
    code = registry.grant()
    assert client.post("/api/agents/session", json={"code": code}, headers=headers).status_code == 403
    assert unlock(client, registry).status_code == 200
    assert client.put(f"/api/agents/{AGENT}", json=config(), headers=headers).status_code == 403
    assert client.post("/api/agents/voices/refresh", json={}, headers=headers).status_code == 403
    assert client.post("/api/agents/voices/clone", headers=headers).status_code == 403
    assert provider.lists == 0 and provider.clones == []


def test_origin_and_marker_alone_never_authorize_execution_or_provider(tmp_path):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider)
    assert client.put(f"/api/agents/{AGENT}", json=config(), headers=HEADERS).status_code == 403
    assert client.post("/api/agents/voices/refresh", json={}, headers=HEADERS).status_code == 403
    assert client.post("/api/agents/voices/clone", headers=HEADERS).status_code == 403
    assert provider.lists == 0 and provider.clones == []


def test_disabled_feature_has_no_mutations_or_provider_calls(tmp_path):
    provider = Provider()
    client, registry = client_for(tmp_path, enabled=False, provider=provider)
    code = registry.grant()
    assert client.get("/api/agents/config").json()["enabled"] is False
    assert client.post("/api/agents/session", json={"code": code}, headers=HEADERS).status_code == 503
    assert client.put(f"/api/agents/{AGENT}", json=config(), headers=HEADERS).status_code == 503
    assert client.post("/api/agents/voices/refresh", json={}, headers=HEADERS).status_code == 503
    assert provider.lists == 0


def test_owner_can_save_unassigned_config_without_voice_credentials(tmp_path):
    client, registry = client_for(tmp_path)
    assert unlock(client, registry).status_code == 200
    response = client.put(f"/api/agents/{AGENT}", json=config(slot=None, voiceProfileId=None, expectedRevision=0), headers=HEADERS)
    assert response.status_code == 200 and response.json()["revision"] == 1
    assert client.post("/api/agents/voices/refresh", json={}, headers=HEADERS).status_code == 503
    data = client.get("/api/agents/config").json()
    assert len(data["agents"]) == 1
    assert data["capabilities"] == {"voiceCatalog": False, "voiceCloning": False}


def test_catalog_then_publish_returns_immutable_revision(tmp_path):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider)
    unlock(client, registry)
    assert client.post("/api/agents/voices/refresh", json={}, headers=HEADERS).status_code == 200
    response = client.put(f"/api/agents/{AGENT}", json=config(slot=9, expectedRevision=0), headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["slot"] == 9 and response.json()["voiceId"] == voice()["voiceId"]
    assert registry.resolve_slot("9").prompt == config()["prompt"]


def test_json_duplicate_fields_large_body_and_unknown_fields_rejected(tmp_path):
    client, registry = client_for(tmp_path)
    unlock(client, registry)
    headers = {**HEADERS, "Content-Type": "application/json"}
    for body, status in [(b'{"name":"x","name":"y"}', 400), (b"x" * (MAX_JSON + 1), 413)]:
        assert client.put(f"/api/agents/{AGENT}", content=body, headers=headers).status_code == status
    assert client.put(f"/api/agents/{AGENT}", json={**config(), "voiceId": "rogue"}, headers=HEADERS).status_code == 400
    assert registry.snapshot()["agents"] == []


def clone(client, *, data=None, files=None, headers=None):
    return client.post("/api/agents/voices/clone", data=data or {"name": "My voice", "consent": "true"},
                       files=files or [("files", ("voice.wav", WAV, "audio/wav"))], headers=headers or HEADERS)


def test_clone_requires_explicit_consent_and_persists_unverified_readiness(tmp_path):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider)
    unlock(client, registry)
    assert clone(client, data={"name": "My voice", "consent": "false"}).status_code == 400
    assert clone(client, data={"name": "My voice"}).status_code == 400
    assert provider.clones == []
    response = clone(client)
    assert response.status_code == 201 and response.json()["requiresVerification"]
    assert not response.json()["ready"]
    assert provider.clones == [("My voice", [("voice.wav", WAV, "audio/wav")])]
    assert client.put(f"/api/agents/{AGENT}", json=config(), headers=HEADERS).status_code == 409
    assert len(AgentRegistry(str(tmp_path)).snapshot()["voices"]) == 1


@pytest.mark.parametrize("filename,mime,body", [("../../voice.wav", "audio/wav", WAV),
                                              ("voice.txt", "text/plain", WAV),
                                              ("voice.wav", "audio/wav", b"not an audio file"),
                                              ("voice.wav", "text/plain", WAV),
                                              ("voice.mp3", "audio/mpeg", b"")])
def test_clone_rejects_unsafe_filename_mime_or_format(tmp_path, filename, mime, body):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider)
    unlock(client, registry)
    assert clone(client, files=[("files", (filename, body, mime))]).status_code in (400, 413, 415)
    assert provider.clones == []


def test_clone_bounds_request_size_file_count_and_each_file(tmp_path):
    provider = Provider()
    client, registry = client_for(tmp_path, provider=provider)
    unlock(client, registry)
    response = client.post("/api/agents/voices/clone", content=b"", headers={**HEADERS,
        "Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(MAX_UPLOAD + 1)})
    assert response.status_code == 413
    assert clone(client, files=[("files", ("v.wav", WAV, "audio/wav"))] * 4).status_code == 400
    assert clone(client, files=[("files", ("v.wav", WAV + b"x" * MAX_SAMPLE, "audio/wav"))]).status_code == 413
    assert provider.clones == []


def test_unauthed_upload_rejected_before_reading_body(tmp_path):
    client, _ = client_for(tmp_path, provider=Provider())
    response = client.post("/api/agents/voices/clone", content=b"", headers={**HEADERS,
        "Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(MAX_UPLOAD + 1)})
    assert response.status_code == 403


def test_published_agent_browser_edits_require_the_opened_revision(tmp_path):
    client, registry = client_for(tmp_path, demo=True)
    registry.add_voice(voice())
    path = f"/api/agents/{AGENT}"
    first = client.put(path, json=config(expectedRevision=0), headers=HEADERS)
    assert first.status_code == 200 and first.json()["revision"] == 1
    current = client.put(path, json=config(expectedRevision=1, prompt="Latest settings"), headers=HEADERS)
    assert current.status_code == 200 and current.json()["revision"] == 2
    for body in (config(), config(expectedRevision=0), config(expectedRevision=1)):
        rejected = client.put(path, json=body, headers=HEADERS)
        assert rejected.status_code == 409
        assert "changed in another browser" in rejected.json()["detail"]
    assert registry.resolve_slot("1").prompt == "Latest settings"
    reviewed = client.put(path, json=config(expectedRevision=2, prompt="Reviewed edit"), headers=HEADERS)
    assert reviewed.status_code == 200 and reviewed.json()["revision"] == 3
