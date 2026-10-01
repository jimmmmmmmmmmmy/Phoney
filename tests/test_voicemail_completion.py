"""Focused product and boundary checks; test helpers live in support."""

import asyncio
import json
import xml.etree.ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_service.routes import register_operator_routes
from operator_service.sessions import OperatorSessions
from voicemail import VoicemailStore

from support.call_history import sid
from support.media_webhooks import signed_post
from support.operator_routes import REMOTE_SID
from support.voicemail_completion import Dialer, RECORDING, receipt_store


def test_signed_native_recording_callbacks_survive_restart_and_play_in_inbox(tmp_path):
    config, receipts = receipt_store(tmp_path)
    async def seed_receipt():
        receipts.start(REMOTE_SID, 'owner-no-answer', mode='voicemail_ai')
        receipts.fallback(REMOTE_SID, 'dialogue-timeout')
        await receipts.close()
    asyncio.run(seed_receipt())
    # No live session is necessary for delayed signed recording callbacks.
    receipts = VoicemailStore(config)
    app, sessions, dialer = FastAPI(), OperatorSessions(config), Dialer()
    register_operator_routes(app, config, sessions, dialer=dialer, voicemail_store=receipts)
    path = f'/twilio/voicemail/recording/{REMOTE_SID}'
    payload = {'AccountSid':config.account_sid, 'CallSid':REMOTE_SID,
        'RecordingSid':RECORDING, 'RecordingStatus':'completed', 'RecordingDuration':'9',
        'RecordingSource':'RecordVerb', 'RecordingUrl':'https://attacker.invalid/secret'}
    with TestClient(app, base_url=config.public_base_url) as client:
        assert client.post(path, data=payload).status_code == 403
        assert signed_post(client,config,path,payload|{'CallSid':sid(99)}).status_code == 400
        assert signed_post(client,config,path,payload|{'RecordingSource':'Conference'}).status_code == 400
        assert signed_post(client,config,path,payload).status_code == 204
        assert signed_post(client,config,path,payload|{'RecordingStatus':'in-progress'}).status_code == 204
        assert signed_post(client,config,path,payload|{'RecordingSid':'RE'+'6'*32}).status_code == 400
        finished = signed_post(client,config,f'/twilio/voicemail/finished/{REMOTE_SID}',
            {'AccountSid':config.account_sid,'CallSid':REMOTE_SID})
        assert finished.status_code == 200 and ET.fromstring(finished.text).find('Hangup') is not None
        result = client.get(f'/api/voicemails/{REMOTE_SID}/audio')
        assert result.status_code == 200 and result.content[:4] == b'RIFF'
        assert result.headers['cache-control'] == 'no-store'
        assert dialer.downloaded == [RECORDING]
        assert client.get(f'/api/voicemails/{sid(99)}/audio').status_code == 404
        record = receipts.get(REMOTE_SID)
        assert record['recording_status'] == 'completed' and record['duration_seconds'] == 9
        assert 'attacker' not in json.dumps(record)
        client.portal.call(receipts.close)
    assert VoicemailStore(config).get(REMOTE_SID)['recording_sid'] == RECORDING
