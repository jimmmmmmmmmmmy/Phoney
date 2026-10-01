"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from dataclasses import replace


import json


import struct


import xml.etree.ElementTree as ET


from fastapi import FastAPI


from fastapi.testclient import TestClient


from operator_service.routes import register_operator_routes


from operator_service.sessions import OperatorSessions


from voicemail import VoicemailStore


from support.operator_routes import SETTINGS, REMOTE_SID


from support.media_webhooks import signed_post


from support.call_history import sid


RECORDING = 'RE' + '5' * 32


def receipt_store(tmp_path):
    config = replace(SETTINGS, voicemail_enabled=True,
                     voicemail_storage_dir=str(tmp_path / 'voicemails'))
    return config, VoicemailStore(config)


class Dialer:
    def __init__(self):
        self.downloaded = []
        self.ended = []
    async def end_call(self, call_sid):
        self.ended.append(call_sid)
    async def recording_audio(self, recording_sid):
        self.downloaded.append(recording_sid)
        return b'RIFF' + struct.pack('<I', 36) + b'WAVE' + b'\0' * 32
