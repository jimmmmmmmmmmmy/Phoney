"""Persisted AI inbox classification and provider-free native Record recovery."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import json
import struct
import xml.etree.ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from call_history import page
from operator_service.routes import register_operator_routes, TwilioLegs, voicemail_recording_twiml
from operator_service.sessions import OperatorSessions
from voicemail import VoicemailStore
from test_operator_routes import SETTINGS, REMOTE_SID
from test_media_webhooks import signed_post
from test_call_history import seed, viewer, sid, NUMBER

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


def test_ai_receipt_persists_outside_legacy_flag_and_uses_local_audio_status(tmp_path):
    config = SimpleNamespace(voicemail_enabled=False, voicemail_agent_enabled=True,
        voicemail_storage_dir='', transcript_storage_dir=str(tmp_path/'transcripts'),
        voicemail_max_seconds=120)
    async def run():
        store = VoicemailStore(config)
        assert store.enabled and store.path == str(tmp_path/'voicemails')
        assert store.start(REMOTE_SID, 'owner-no-answer', mode='voicemail_ai',
                           started_at='2026-09-27T10:00:00+00:00')
        store.finish_ai(REMOTE_SID, available=True, duration=185.8)
        await store.close()
        fresh = VoicemailStore(config)
        record = fresh.get(REMOTE_SID)
        assert record['mode'] == 'voicemail_ai' and record['recording_sid'] == ''
        assert record['recording_status'] == 'completed' and record['duration_seconds'] == 185
        assert fresh.archive_snapshot()['voicemails'] == [record]
        await fresh.close()
    asyncio.run(run())


def test_failed_local_capture_does_not_claim_playable_recording(tmp_path):
    async def run():
        _, store = receipt_store(tmp_path)
        store.start(REMOTE_SID, 'owner-no-answer', mode='voicemail_ai')
        store.finish_ai(REMOTE_SID, available=False)
        assert store.get(REMOTE_SID)['recording_status'] == 'failed'
        assert store.get(REMOTE_SID)['ended_at']
        await store.close()
    asyncio.run(run())


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


def test_fallback_twiml_keeps_same_call_and_uses_no_ai_provider(tmp_path):
    config, _ = receipt_store(tmp_path)
    xml = ET.fromstring(voicemail_recording_twiml(config, REMOTE_SID))
    assert xml.find('Say') is not None and xml.find('Record') is not None
    assert xml.find('Connect') is None and xml.find('Dial') is None
    assert xml.find('Record').attrib['maxLength'] == '120'
    assert xml.find('Record').attrib['timeout'] == '5'
    assert xml.find('Record').attrib['transcribe'] == 'false'
    assert xml.find('Record').attrib['action'].endswith('/'+REMOTE_SID)


@pytest.mark.parametrize('status', ['completed', 'absent', 'failed'])
def test_terminal_recording_callback_alone_releases_only_matching_fallback(tmp_path, status):
    config, receipts = receipt_store(tmp_path)
    config = SimpleNamespace(**(vars(config) | {
        'operator_inbound_enabled': True, 'voicemail_agent_enabled': True}))
    app, sessions, dialer = FastAPI(), OperatorSessions(config, max_active=1), Dialer()
    register_operator_routes(app, config, sessions, dialer=dialer, voicemail_store=receipts)
    async def fallback_session():
        session, _ = await sessions.reserve_inbound(REMOTE_SID, '+12025550199')
        assert await sessions.claim_voicemail(session.id)
        session.voicemail_fallback = True
        receipts.start(REMOTE_SID, 'owner-no-answer', mode='voicemail_ai')
        receipts.fallback(REMOTE_SID, 'provider-unavailable')
        return session
    path = f'/twilio/voicemail/recording/{REMOTE_SID}'
    payload = {'AccountSid': config.account_sid, 'CallSid': REMOTE_SID,
        'RecordingSid': RECORDING, 'RecordingStatus': status, 'RecordingDuration': '9'}
    with TestClient(app, base_url=config.public_base_url) as client:
        session = client.portal.call(fallback_session)
        assert sessions.active_count == 1
        # Neither the Record action nor a call-status callback is delivered.
        assert signed_post(client, config, path, payload).status_code == 204
        assert not session.active and sessions.active_count == 0
        assert receipts.get(REMOTE_SID)['recording_status'] == status
        newer, _ = client.portal.call(sessions.reserve_inbound, sid(99), '+12025550188')
        assert newer.active
        assert signed_post(client, config, path, payload).status_code == 204
        assert signed_post(client, config, path, payload | {'RecordingStatus': 'in-progress'}).status_code == 204
        assert newer.active and sessions.active_count == 1
        assert receipts.get(REMOTE_SID)['recording_status'] == status
        client.portal.call(sessions.close)
        client.portal.call(receipts.close)


def test_voicemail_history_filters_before_paging_and_preserves_contact_totals(tmp_path):
    seed(tmp_path,45)
    _, manager, details, recordings = viewer(tmp_path)
    receipts = {'enabled':True, 'storage_error':'', 'voicemails':[
        {'call_sid':sid(i),'mode':'voicemail_ai','recording_status':'completed'} for i in range(1,25)]}
    first = page(manager,details,recordings,manager.snapshot(),receipts,collection='voicemail')
    assert first['history']['total'] == 24
    assert first['history']['counts'] == {'recent':21,'voicemail':24}
    assert first['history']['caller_metrics'][NUMBER]['total'] == 45
    assert {item['call_sid'] for item in first['sessions']} == {sid(i) for i in range(5,25)}
    second = page(manager,details,recordings,manager.snapshot(),receipts,collection='voicemail',
                  cursor=first['history']['next_cursor'])
    assert {item['call_sid'] for item in second['sessions']} == {sid(i) for i in range(1,5)}
    assert not second['history']['has_more']
    with pytest.raises(ValueError):
        page(manager,details,recordings,manager.snapshot(),receipts,collection='recent',
             cursor=first['history']['next_cursor'])
    recent = page(manager,details,recordings,manager.snapshot(),receipts,collection='recent')
    assert recent['history']['total'] == 21 and not recent['voicemail']['voicemails']
    mixed = page(manager,details,recordings,manager.snapshot(),receipts)
    assert mixed['history']['total'] == 45


@pytest.mark.parametrize('kind',['good','non-audio','oversized','redirect','unavailable'])
def test_native_audio_adapter_uses_fixed_sid_endpoint_and_bounds_provider_data(tmp_path,monkeypatch,kind):
    config,_=receipt_store(tmp_path)
    requests=[]
    wav=b'RIFF'+struct.pack('<I',36)+b'WAVE'+b'\0'*32
    maximum=(config.voicemail_max_seconds+5)*32000+65536
    async def handle(request):
        requests.append(request)
        assert request.url.host=='api.twilio.com'
        assert request.url.path==f'/2010-04-01/Accounts/{config.account_sid}/Recordings/{RECORDING}.wav'
        assert request.headers.get('authorization','').startswith('Basic ')
        if kind=='redirect':return httpx.Response(302,headers={'location':'https://other.invalid/private'})
        if kind=='unavailable':return httpx.Response(503,text='private-provider-message')
        return httpx.Response(200,content=(wav if kind=='good' else b'bad' if kind=='non-audio'
                                          else b'x'*(maximum+1)))
    original=httpx.AsyncClient
    monkeypatch.setattr('operator_service.routes.httpx.AsyncClient',
                        lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    async def run():
        adapter=TwilioLegs(config)
        if kind=='good':
            assert await adapter.recording_audio(RECORDING)==wav
        else:
            with pytest.raises((httpx.HTTPStatusError,ValueError)):
                await adapter.recording_audio(RECORDING)
        assert len(requests)==1
    asyncio.run(run())


def test_corrupt_receipt_mode_does_not_crash_store(tmp_path):
    from transcription import storage
    from test_voicemail_archive import receipt
    config,_=receipt_store(tmp_path)
    damaged=receipt(5)|{'mode':[]}
    storage.save(config.voicemail_storage_dir,damaged)
    store=VoicemailStore(config)
    assert store.get(damaged['call_sid']) is None
    assert store.archive_snapshot()['storage_error']=='load-failed'
