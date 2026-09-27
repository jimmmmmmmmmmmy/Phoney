"""Bounded, owner-triggered ElevenLabs catalog and enrollment requests.

Catalog: https://elevenlabs.io/docs/api-reference/voices/search
Enrollment uses the existing voice_stack.tts CLONE_URL contract. Neither
operation synthesizes speech or changes call routing.
"""

import asyncio
import json

import httpx

from voice_stack.tts import CLONE_URL
from .store import RegistryError, MAX_VOICES

MAX_RESPONSE = 2 * 1024 * 1024


def voice_record(value, *, name=None, cloning=False):
    if not isinstance(value, dict):
        raise RegistryError("The voice provider returned an invalid response.", 502)
    verification = value.get("voice_verification") or {}
    if not isinstance(verification, dict):
        raise RegistryError("The voice provider returned an invalid verification status.", 502)
    required = value.get("requires_verification", verification.get("requires_verification"))
    verified = verification.get("is_verified") is True
    category = value.get("category", "")
    if required is not None and type(required) is not bool:
        raise RegistryError("The voice provider returned an invalid verification status.", 502)
    # A missing flag must not approve a clone simply because catalog refresh
    # omitted its verification record. Built-in/generated voices need no clone
    # verification; unknown categories remain unavailable until verified.
    needs_verification = ((required is not False and not verified)
                          if cloning or category not in ("premade", "generated")
                          else required is True and not verified)
    fine_tuning = value.get("fine_tuning") or {}
    if not isinstance(fine_tuning, dict):
        fine_tuning = {}
    states = fine_tuning.get("state") or {}
    trained = isinstance(states, dict) and "fine_tuned" in states.values()
    ready = not needs_verification and (category != "professional" or trained)
    if value.get("safety_control") not in (None, "NONE"):
        ready = False
    return {"voiceId": value.get("voice_id"), "name": name if name is not None else value.get("name"),
            "ready": ready, "requiresVerification": needs_verification}


class VoiceProvider:
    def __init__(self, api_key, *, transport=None):
        self.api_key = api_key
        self.transport = transport

    async def _request(self, client, method, url, **kwargs):
        async with client.stream(method, url, headers={"xi-api-key": self.api_key}, **kwargs) as response:
            if response.status_code != 200:
                # Never return the provider body: it may contain account details.
                raise RegistryError(f"Voice provider request failed (HTTP {response.status_code}).", 502)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE:
                    raise RegistryError("The voice provider response was too large.", 502)
                body.extend(chunk)
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError):
                raise RegistryError("The voice provider returned an invalid response.", 502) from None
            if not isinstance(value, dict):
                raise RegistryError("The voice provider returned an invalid response.", 502)
            return value

    async def list_voices(self):
        try:
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(20, connect=5)) as client:
                    records, token, seen = [], None, set()
                    for _ in range(6):
                        params = {"page_size": 100}
                        if token:
                            params["next_page_token"] = token
                        data = await self._request(client, "GET", "https://api.elevenlabs.io/v2/voices", params=params)
                        voices = data.get("voices")
                        if not isinstance(voices, list):
                            raise RegistryError("The voice provider returned an invalid catalog.", 502)
                        records.extend(voice_record(value) for value in voices)
                        if len(records) > MAX_VOICES:
                            raise RegistryError("The voice catalog exceeds 500 voices.", 502)
                        if not data.get("has_more"):
                            return records
                        token = data.get("next_page_token")
                        if not isinstance(token, str) or not token or len(token) > 1000 or token in seen:
                            raise RegistryError("The voice catalog could not be fully loaded.", 502)
                        seen.add(token)
                    raise RegistryError("The voice catalog could not be fully loaded.", 502)
        except (httpx.HTTPError, TimeoutError):
            raise RegistryError("The voice provider could not be reached. Try again.", 502) from None

    async def clone(self, name, samples):
        try:
            async with asyncio.timeout(120):
                async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(90, connect=5)) as client:
                    data = await self._request(client, "POST", CLONE_URL, data={"name": name},
                                               files=[("files", sample) for sample in samples])
                    return voice_record(data, name=name, cloning=True)
        except (httpx.HTTPError, TimeoutError):
            raise RegistryError("Voice enrollment could not be confirmed. Refresh the voice list before retrying.", 502) from None
