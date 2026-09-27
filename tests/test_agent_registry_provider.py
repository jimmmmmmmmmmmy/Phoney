"""Provider catalog paging and readiness, exercised with HTTPX mock transport."""

import asyncio
import json

import httpx
import pytest

from agent_registry.provider import VoiceProvider, voice_record, MAX_RESPONSE
from agent_registry.store import RegistryError


def test_catalog_pages_bounded_and_returns_only_safe_profile_fields():
    requests = []

    def handler(request):
        requests.append(request)
        token = request.url.params.get("next_page_token")
        return httpx.Response(200, json={"voices": [{"voice_id": "secondVoice" if token else "firstVoice",
            "name": "Voice", "category": "generated", "preview_url": "private", "settings": {"secret": "not returned"}}],
            "has_more": not token, "next_page_token": "next"})

    records = asyncio.run(VoiceProvider("secret-key", transport=httpx.MockTransport(handler)).list_voices())
    assert len(records) == len(requests) == 2
    assert records[0] == {"voiceId": "firstVoice", "name": "Voice", "ready": True, "requiresVerification": False}
    assert requests[0].url.path == "/v2/voices"
    assert requests[0].headers["xi-api-key"] == "secret-key"
    assert "private" not in json.dumps(records)


@pytest.mark.parametrize("payload,expected", [
    ({"category": "cloned"}, False),
    ({"category": "cloned", "voice_verification": {"requires_verification": True, "is_verified": False}}, False),
    ({"category": "cloned", "voice_verification": {"requires_verification": False}}, True),
    ({"category": "cloned", "voice_verification": {"requires_verification": True, "is_verified": True}}, True),
    ({"category": "professional", "voice_verification": {"is_verified": True}}, False),
    ({"category": "professional", "voice_verification": {"is_verified": True},
      "fine_tuning": {"state": {"eleven_multilingual_v2": "fine_tuned"}}}, True),
    ({"category": "generated", "safety_control": "BLOCKED"}, False),
])
def test_readiness_refuses_unverified_or_untrained_voices(payload, expected):
    assert voice_record({"voice_id": "voice123", "name": "Example", **payload})["ready"] is expected


def test_clone_missing_verification_never_means_ready():
    assert not voice_record({"voice_id": "voice123"}, name="Example", cloning=True)["ready"]
    assert voice_record({"voice_id": "voice123", "requires_verification": False}, name="Example", cloning=True)["ready"]


@pytest.mark.parametrize("response", [httpx.Response(401, text="sensitive account details"),
                                     httpx.Response(200, text="not JSON"),
                                     httpx.Response(200, json={"voices": "not a list"}),
                                     httpx.Response(200, content=b"x" * (MAX_RESPONSE + 1))])
def test_provider_errors_redact_body_and_refuse_invalid_shapes(response):
    provider = VoiceProvider("secret-key", transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(RegistryError) as error:
        asyncio.run(provider.list_voices())
    assert "sensitive" not in str(error.value) and "secret-key" not in str(error.value)


def test_repeated_pagination_token_fails_instead_of_looping():
    provider = VoiceProvider("key", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "voices": [], "has_more": True, "next_page_token": "same"})))
    with pytest.raises(RegistryError):
        asyncio.run(provider.list_voices())


def test_clone_uses_encoded_files_with_consent_managed_upstream():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"voice_id": "cloneABC", "requires_verification": True})

    provider = VoiceProvider("key", transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.clone("My voice", [("sample.wav", b"RIFFsample", "audio/wav")]))
    assert requests[0].url.path == "/v1/voices/add"
    assert b"RIFFsample" in requests[0].content
    assert result["requiresVerification"] and not result["ready"]
