"""Focused product and boundary checks; test helpers live in support."""

from operator_service.sessions import CONNECTED, ENDED, HUMAN, OWNER, OWNER_PROMPT, REMOTE

from support.media_webhooks import signed_post
from support.operator_routes import ACCOUNT, DESTINATION, HEADERS, OWNER_FRAME, REMOTE_FRAME, REMOTE_STREAM, accept_owner, await_frame, bridge_client, connect, connected, keypad, media_message, session_of, settle, start_call, start_message, until


def test_owner_presses_one_and_both_people_hear_each_other():
    with bridge_client() as (client, dialer, settings):
        body = start_call(client).json()
        session_id = body["session_id"]
        settle(client)
        with connect(client, settings, session_id, OWNER) as owner:
            accept_owner(client, settings, session_id, owner)
            assert session_of(client, session_id).phase == OWNER_PROMPT
            owner.send_json(keypad("5", 2))         # only a bare 1 accepts
            settle(client)
            assert len(dialer.created) == 1
            owner.send_json(keypad("1", 3))
            until(client, lambda: len(dialer.created) == 2)
            assert session_of(client, session_id).phase == "remote_setup"
            assert len(dialer.created) == 2
            assert dialer.created[1]["to"] == DESTINATION
            assert "Press 1" not in dialer.created[1]["twiml"]
            assert f'wss://operator.example/media/{session_id}/remote/' in dialer.created[1]["twiml"]
            assert client.app.state.operator_controller.routers[session_id].cue is not None
            # A repeated command must not create a second remote call.
            owner.send_json(keypad("1", 4))
            until(client, lambda: session_of(client, session_id).legs[OWNER].counters["dtmf"] == 3)
            assert len(dialer.created) == 2
            with connect(client, settings, session_id, REMOTE) as remote:
                remote.send_json(connected())
                remote.send_json(start_message(client, session_id, REMOTE))
                until(client, lambda: session_of(client, session_id).phase == CONNECTED)
                assert client.app.state.operator_controller.routers[session_id].cue is None
                owner.send_json(media_message(OWNER_FRAME, sequence="5"))
                await_frame(client, remote, OWNER_FRAME)
                remote.send_json(media_message(REMOTE_FRAME, role=REMOTE, sequence="6"))
                await_frame(client, owner, REMOTE_FRAME)
                # The dealership's own keypad cannot change anything.
                remote.send_json({"event": "dtmf", "sequenceNumber": "7",
                                  "streamSid": REMOTE_STREAM,
                                  "dtmf": {"track": "inbound_track", "digit": "#1"}})
                settle(client)
                assert session_of(client, session_id).mode == HUMAN
                assert len(dialer.created) == 2
                owner_sid = session_of(client, session_id).legs[OWNER].call_sid
                remote_sid = session_of(client, session_id).legs[REMOTE].call_sid
                status = client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()
                assert status["legs"][OWNER]["counters"]["dtmf"] == 3
                assert status["legs"][REMOTE]["counters"]["frames_in"] == 1
                body = client.get(f"/api/sessions/{session_id}", headers=HEADERS).text
                assert owner_sid not in body and remote_sid in body
                assert status["canonical_call_sid"] == remote_sid
                ended = signed_post(client, settings, f"/twilio/status/{session_id}/owner",
                                    {"AccountSid": ACCOUNT, "CallSid": owner_sid,
                                     "CallStatus": "completed", "CallDuration": "62"})
                assert ended.status_code == 204
                settle(client)
                assert session_of(client, session_id).phase == ENDED
                # Only the surviving leg is hung up; the owner's call already ended.
                assert dialer.ended == [remote_sid]
                final = client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()
                assert final["phase"] == ENDED and final["ended_reason"] == "owner-completed"
                assert final["legs"][OWNER]["call_status"] == "completed"
                # The already-closed session ignores later callbacks instead of reopening.
                assert signed_post(client, settings, f"/twilio/status/{session_id}/remote",
                                   {"AccountSid": ACCOUNT, "CallSid": remote_sid,
                                    "CallStatus": "completed"}).status_code == 204
                settle(client)
                assert dialer.ended == [remote_sid]
