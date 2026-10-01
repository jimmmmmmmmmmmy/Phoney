"""Shared fixtures and fakes for focused integration checks."""

from contextlib import contextmanager


from dataclasses import replace


import json


from fastapi.testclient import TestClient


from app import create_app


from support.media_webhooks import (
    Gateway,
    PARENT,
    SETTINGS,
    assert_socket_closed,
    read_manifest,
    send_audio,
    send_start,
    socket_headers,
    stop_message,
    stream_and_token,
)


from support.transcription import Connector, result, until


@contextmanager
def live_client(tmp_path, *, options=None):
    settings = replace(SETTINGS, media_capture_enabled=True,
                       media_storage_dir=str(tmp_path / "captures"),
                       transcription_enabled=True, deepgram_api_key="fixture-private-key",
                       transcript_storage_dir=str(tmp_path / "transcripts"))
    gateway, connector = Gateway(), Connector(options)
    app = create_app(settings, gateway=gateway, transcription_connector=connector)
    with TestClient(app, base_url=settings.public_base_url) as client:
        yield client, settings, gateway, connector


def wait_for(client, predicate):
    client.portal.call(until, predicate)


def final_session(client):
    return client.app.state.transcription.snapshot()["sessions"][0]
