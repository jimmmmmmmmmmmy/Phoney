"""Focused product and boundary checks; test helpers live in support."""

from concurrent.futures import ThreadPoolExecutor

from workspace_store import WorkspaceConflict, WorkspaceStore

from support.workspace_store import contact


def test_concurrent_updates_to_same_revision_have_one_winner(tmp_path):
    directory = str(tmp_path / "workspace")
    saved = WorkspaceStore(directory).put_contact(contact()["id"], contact())

    def edit(number):
        try:
            return WorkspaceStore(directory).put_contact(saved["id"], {**saved, "firstName": str(number)})
        except WorkspaceConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(edit, range(8)))
    winners = [value for value in results if value]
    assert len(winners) == 1 and winners[0]["revision"] == 2
