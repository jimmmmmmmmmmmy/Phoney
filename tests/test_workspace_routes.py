"""Focused product and boundary checks; test helpers live in support."""

from workspace_store import WorkspaceStore

from support.workspace_routes import AGENT, agent, client_for, contact, put_agent, put_contact


def test_contact_agent_and_edits_survive_restart_and_reach_another_browser(tmp_path):
    directory = str(tmp_path / "shared")
    with client_for(WorkspaceStore(directory)) as first, client_for(WorkspaceStore(directory)) as second:
        response = put_contact(first)
        assert response.status_code == 200
        assert response.json() == {**contact(), "revision": 1}
        assert put_agent(first).json() == {**agent(), "revision": 1}
        # A store that existed before the write must observe other browser edits.
        assert second.get("/api/workspace").json() == {
            "version": 1, "contacts": [{**contact(), "revision": 1}], "demoOverrides": [], "agents": [{**agent(), "revision": 1}]}
        changed = contact(firstName="Updated", labels=["Legal"], revision=1)
        changed = put_contact(second, changed).json()
        assert changed["revision"] == 2
        assert first.get("/api/workspace").json()["contacts"] == [changed]
    with client_for(WorkspaceStore(directory), base_url="https://new-tunnel.example") as restarted:
        assert restarted.get("/api/workspace").json()["contacts"] == [changed]
        updated_agent = agent(prompt="Confirm the appointment time.", revision=1)
        response = restarted.put("/api/workspace/agents/" + AGENT, json=updated_agent,
                                 headers={"Origin": "https://new-tunnel.example", "X-Workspace-Request": "1"})
        assert response.status_code == 200 and response.json() == {**updated_agent, "revision": 2}
