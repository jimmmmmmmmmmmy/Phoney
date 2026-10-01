"""Focused product and boundary checks; test helpers live in support."""

import asyncio

from call_details import CallDetailsStore

from support.summary_pair import BRIEF, CALL, DETAILED, DISPLAY_BRIEF, DISPLAY_DETAILED, DualProvider, disk_record, manager_for, public_record


def test_two_polls_create_two_distinct_outputs_and_restart_never_duplicates(tmp_path):
    async def run():
        store, provider = CallDetailsStore(str(tmp_path)), DualProvider()
        manager = manager_for(tmp_path, store, provider)
        try:
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed"]
            first = public_record(store)
            assert first["summary"]["text"] == DISPLAY_DETAILED
            assert first["brief_summary"] is None
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
            pair = public_record(CallDetailsStore(str(tmp_path)))
            for key, expected in (("summary", DISPLAY_DETAILED), ("brief_summary", DISPLAY_BRIEF)):
                assert pair[key]["text"] == expected
                assert pair[key]["source"] == "gemini"
                assert pair[key]["model"] == "gemini-3.8-flash"
                assert pair[key]["created_at"]
                assert pair[key + "_status"] == "completed"
                assert "fingerprint" not in pair[key]
            assert disk_record(tmp_path)["summary_job"]["attempts"] == 1
            assert disk_record(tmp_path)["brief_summary_job"]["attempts"] == 1
            assert disk_record(tmp_path)["summary"]["text"] == DETAILED
            assert disk_record(tmp_path)["brief_summary"]["text"] == BRIEF
        finally:
            await manager.close()
        restarted_provider = DualProvider()
        original_bytes = (tmp_path / (CALL + ".json")).read_bytes()
        restarted_store = CallDetailsStore(str(tmp_path))
        restarted = manager_for(tmp_path, restarted_store, restarted_provider)
        try:
            for _ in range(4):
                await restarted.run_once()
            assert restarted_provider.calls == []
            displayed = public_record(restarted_store)
            assert displayed["summary"]["text"] == DISPLAY_DETAILED
            assert displayed["brief_summary"]["text"] == DISPLAY_BRIEF
            assert (tmp_path / (CALL + ".json")).read_bytes() == original_bytes
        finally:
            await restarted.close()

    asyncio.run(run())
