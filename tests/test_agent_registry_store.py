"""Owner-published agents survive restarts without trusting public draft edits."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import sqlite3
import stat

import pytest

from agent_registry import AgentRegistry, RegistryError
from agent_registry.store import DATABASE_NAME, SESSION_SECONDS, DEFAULT_AGENT_ID, DEFAULT_PERSONALITY, LEGACY_DEFAULT_PERSONALITY
from workspace_store import WorkspaceStore

AGENT = "agent-12345678-1234-1234-1234-123456789abc"
OTHER = "agent-abcdefgh-1234-1234-1234-123456789abc"


def voice(**changes):
    return {"name": "Owner voice", "voiceId": "voiceABC123456789", "ready": True,
            "requiresVerification": False, **changes}


def config(**changes):
    return {"name": "Reception", "prompt": "Ask how we can help.",
            "voiceProfileId": "voice-voiceABC123456789", "slot": 1, **changes}


def ready_store(tmp_path):
    store = AgentRegistry(str(tmp_path / "workspace"))
    store.add_voice(voice())
    return store


def test_default_voice_clone_is_persistent_and_does_not_reset_edits(tmp_path):
    store = ready_store(tmp_path)
    store.add_voice(voice(name="owner"))
    seeded = store.ensure_default_voice_clone()
    assert seeded.id == DEFAULT_AGENT_ID
    assert (seeded.name, seeded.prompt, seeded.slot) == ("Voice Clone", DEFAULT_PERSONALITY, 1)
    assert store.resolve_slot("1") == seeded
    assert seeded.voice_id == voice()["voiceId"]
    edited = store.publish(seeded.id, config(name="My agent", prompt="Later edit", slot=3))
    restarted = AgentRegistry(str(tmp_path / "workspace"))
    assert restarted.ensure_default_voice_clone() == edited
    assert restarted.resolve_slot("1") is None
    assert restarted.snapshot()["agents"] == [edited.to_dict()]


def test_default_waits_for_ready_owner_voice_without_inventing_a_voice(tmp_path):
    store = ready_store(tmp_path)
    assert store.ensure_default_voice_clone() is None
    store.add_voice(voice(name="owner", requiresVerification=True))
    assert store.ensure_default_voice_clone() is None
    assert store.snapshot()["agents"] == []
    store.add_voice(voice(name="owner"))
    assert store.ensure_default_voice_clone().slot == 1


@pytest.mark.parametrize("personality", [DEFAULT_PERSONALITY, LEGACY_DEFAULT_PERSONALITY])
def test_default_adopts_matching_agent_without_duplicate_or_lost_history(tmp_path, personality):
    store = ready_store(tmp_path)
    store.add_voice(voice(name="owner"))
    prior = store.publish(AGENT, config(name="Tom", prompt=personality + "."))
    seeded = store.ensure_default_voice_clone()
    assert seeded.id == prior.id and seeded.revision == 2
    assert seeded.name == "Voice Clone" and seeded.prompt == DEFAULT_PERSONALITY
    assert len(store.snapshot()["agents"]) == 1
    with sqlite3.connect(tmp_path / "workspace" / DATABASE_NAME) as db:
        first = json.loads(db.execute("SELECT payload FROM revisions WHERE revision=1").fetchone()[0])
    assert first == prior.to_dict()


def test_default_preserves_other_agent_when_assigning_slot_one(tmp_path):
    store = ready_store(tmp_path)
    store.add_voice(voice(name="owner"))
    prior = store.publish(AGENT, config())
    seeded = store.ensure_default_voice_clone()
    assert seeded.id == DEFAULT_AGENT_ID
    saved = {item["id"]: item for item in store.snapshot()["agents"]}
    assert saved[AGENT] == {**prior.to_dict(), "revision": 2, "slot": None}
    assert store.resolve_slot("1").id == DEFAULT_AGENT_ID


def test_public_draft_is_not_execution_and_cannot_change_published_snapshot(tmp_path):
    folder = str(tmp_path / "workspace")
    public = WorkspaceStore(folder)
    draft = {"id": AGENT, "name": "Draft", "prompt": "Original", "createdAt": "2026-09-26T12:00:00Z"}
    draft = public.put_agent(AGENT, draft)
    store = ready_store(tmp_path)
    assert store.snapshot()["agents"] == [] and store.resolve_slot("1") is None
    original = store.publish(AGENT, config())
    public.put_agent(AGENT, {**draft, "prompt": "A public visitor changed this"})
    assert store.resolve_slot("1") == original
    changed = store.publish(AGENT, config(prompt="Owner-approved revision"))
    assert changed.revision == 2
    assert original.prompt == "Ask how we can help."
    with pytest.raises(FrozenInstanceError):
        original.prompt = "mutated"
    with sqlite3.connect(tmp_path / "workspace" / DATABASE_NAME) as db:
        first = json.loads(db.execute("SELECT payload FROM revisions WHERE revision=1").fetchone()[0])
    assert first["prompt"] == original.prompt
    assert public.snapshot()["agents"][0]["prompt"] == "A public visitor changed this"


def test_slots_and_voices_survive_restart_and_snapshot_is_defensive(tmp_path):
    store = ready_store(tmp_path)
    saved = store.publish(AGENT, config(slot=9, prompt="x" * 8000))
    snapshot = store.snapshot()
    snapshot["agents"][0]["prompt"] = "not persisted"
    restarted = AgentRegistry(str(tmp_path / "workspace"))
    assert restarted.resolve_slot("9") == saved
    assert restarted.resolve_slot("0") is None
    assert restarted.resolve_slot(9) is None
    assert len(restarted.snapshot()["voices"]) == 1
    assert stat.S_IMODE((tmp_path / "workspace").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "workspace" / DATABASE_NAME).stat().st_mode) == 0o600


def test_unassigned_config_does_not_require_voice_or_prompt(tmp_path):
    store = ready_store(tmp_path)
    saved = store.publish(AGENT, config(prompt="", slot=None, voiceProfileId=None))
    assert saved.slot is None and saved.voice_id == ""
    assert store.resolve_slot("1") is None


def test_slot_conflict_is_transactional_and_reassignment_releases_previous(tmp_path):
    store = ready_store(tmp_path)
    first = store.publish(AGENT, config())
    with pytest.raises(RegistryError) as error:
        store.publish(OTHER, config())
    assert error.value.status_code == 409
    assert store.snapshot()["agents"] == [first.to_dict()]
    store.publish(AGENT, config(slot=None))
    store.publish(OTHER, config())
    assert store.resolve_slot("1").id == OTHER


def test_concurrent_slot_assignment_has_one_winner(tmp_path):
    store = ready_store(tmp_path)

    def publish(ident):
        try:
            return store.publish(ident, config()).id
        except RegistryError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, [AGENT, OTHER]))
    assert len([item for item in outcomes if item]) == 1


@pytest.mark.parametrize("changes", [{"slot": 0}, {"slot": 10}, {"slot": True}, {"slot": "1"},
                                     {"name": ""}, {"name": "x" * 81}, {"name": "line\nbreak"},
                                     {"prompt": "x" * 8001}, {"voiceProfileId": "../../x"},
                                     {"voiceId": "invented"}])
def test_invalid_agent_configuration_is_atomic(tmp_path, changes):
    store = ready_store(tmp_path)
    with pytest.raises(RegistryError):
        store.publish(AGENT, config(**changes))
    assert store.snapshot()["agents"] == []


@pytest.mark.parametrize("changes", [{"voiceProfileId": None}, {"prompt": ""}])
def test_assigned_agent_needs_ready_voice_and_prompt(tmp_path, changes):
    store = ready_store(tmp_path)
    with pytest.raises(RegistryError):
        store.publish(AGENT, config(**changes))


def test_unverified_and_disappeared_voices_cannot_be_used(tmp_path):
    store = ready_store(tmp_path)
    store.add_voice(voice(requiresVerification=True, ready=True))
    with pytest.raises(RegistryError):
        store.publish(AGENT, config())
    store.add_voice(voice())
    pinned = store.publish(AGENT, config())
    store.replace_voices([])
    assert store.resolve_slot("1") is None
    assert store.snapshot()["voices"][0]["available"] is False
    assert pinned.voice_id == "voiceABC123456789"


def test_invalid_catalog_does_not_partially_update(tmp_path):
    store = ready_store(tmp_path)
    before = store.snapshot()
    with pytest.raises(RegistryError):
        store.replace_voices([voice(name="Changed"), voice(voiceId="bad/id")])
    assert store.snapshot() == before


def test_grants_are_hashed_single_use_short_expiry_and_sessions_revocable(tmp_path):
    now = [100.0]
    store = AgentRegistry(str(tmp_path), clock=lambda: now[0])
    grant = store.grant(ttl=30)
    session = store.consume_grant(grant)
    assert store.authorized(session)
    assert grant.encode() not in (tmp_path / DATABASE_NAME).read_bytes()
    assert session.encode() not in (tmp_path / DATABASE_NAME).read_bytes()
    with pytest.raises(RegistryError):
        store.consume_grant(grant)
    now[0] += SESSION_SECONDS + 1
    assert not store.authorized(session)
    grant = store.grant(ttl=30)
    now[0] += 31
    with pytest.raises(RegistryError):
        store.consume_grant(grant)
    session = store.consume_grant(store.grant())
    store.revoke(session)
    assert not store.authorized(session)
    session = store.consume_grant(store.grant())
    grant = store.grant()
    store.revoke_all()
    assert not store.authorized(session)
    with pytest.raises(RegistryError):
        store.consume_grant(grant)


def test_concurrent_grant_consumption_has_one_winner(tmp_path):
    store = AgentRegistry(str(tmp_path))
    grant = store.grant()

    def consume(_):
        try:
            return store.consume_grant(grant)
        except RegistryError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, [1, 2]))
    assert sum(result is not None for result in results) == 1


def test_database_symlink_is_refused_without_changing_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"private contents")
    folder = tmp_path / "workspace"
    folder.mkdir()
    (folder / DATABASE_NAME).symlink_to(target)
    with pytest.raises(RegistryError):
        AgentRegistry(str(folder)).grant()
    assert target.read_bytes() == b"private contents"


def test_disabled_registry_never_falls_back_to_memory():
    store = AgentRegistry("")
    assert not store.authorized("x" * 43)
    assert store.resolve_slot("1") is None
    with pytest.raises(RegistryError):
        store.grant()
