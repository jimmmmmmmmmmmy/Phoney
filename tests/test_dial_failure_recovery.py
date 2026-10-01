"""Focused product and boundary checks; test helpers live in support."""

import uuid

from operator_service.sessions import CONNECTED, REMOTE

from support.browser_call_routes import browser_connect, browser_start, browser_token, self_call
from support.media_webhooks import signed_post
from support.operator_routes import ACCOUNT, HEADERS, OWNER_NUMBER, bridge_client, connect, session_of, settle, start_call, start_message, until


def test_signed_remote_status_recovers_connection_error_before_default_deadline_without_redial(caplog):
    with bridge_client() as (client, dialer, settings):
        store = client.app.state.operator
        assert store.deadlines == {}
        dialer.failure = ConnectionError("Response lost after Twilio accepted the phone leg")
        request_key = str(uuid.uuid4())
        session_id = self_call(client, request_key)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            settle(client)
            call = session_of(client, session_id)
            assert call.legs[REMOTE].uncertain
            reconciliation = store._timers[(session_id, "reconcile:remote")]
            call_sid = dialer.created[0]["sid"]
            response = signed_post(client, settings, f"/twilio/status/{session_id}/{REMOTE}", {
                "AccountSid": ACCOUNT, "CallSid": call_sid, "CallStatus": "ringing"})
            assert response.status_code == 204
            assert call.legs[REMOTE].call_sid == call_sid
            assert not call.legs[REMOTE].uncertain
            assert (session_id, "reconcile:remote") not in store._timers
            until(client, reconciliation.cancelled)
            duplicate = start_call(client, key=request_key, to=OWNER_NUMBER)
            assert duplicate.json()["session_id"] == session_id
            assert duplicate.json()["duplicate"] is True
            settle(client)
            assert len(dialer.created) == 1
            with connect(client, settings, session_id, REMOTE) as phone:
                phone.send_json(start_message(client, session_id, REMOTE))
                until(client, lambda: call.phase == CONNECTED)
                assert call.canonical_call_sid == call_sid
                assert len(dialer.created) == 1
                assert client.post(f"/api/sessions/{session_id}/end", headers=HEADERS).status_code == 200
                settle(client)
                assert dialer.ended == [call_sid]
        assert "operator task failed type=KeyError" not in caplog.text
