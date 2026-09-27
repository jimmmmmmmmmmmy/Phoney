"""Hook repair uses GitHub's canonical name after a repository rename."""

import json
from types import SimpleNamespace

import pytest

from scripts import configure_github


def test_renamed_repository_hook_update_avoids_mutating_redirect(tmp_path, monkeypatch):
    canonical = "jimmmmmmmmmmmy/Phoney"
    origin = "https://new-tunnel.example"
    current = {"id": 123, "active": True, "events": ["push"],
               "config": {"url": "https://old-tunnel.example/github/webhook",
                          "content_type": "json", "insecure_ssl": "0"}}
    state = tmp_path / "hook.json"
    state.write_text(json.dumps({"id": 123, "repository": configure_github.REPOSITORY}))
    monkeypatch.setattr(configure_github, "STATE", state)
    monkeypatch.setattr(configure_github.sys, "argv", ["configure_github.py", "--apply"])
    monkeypatch.setattr(configure_github.Settings, "from_env", lambda: SimpleNamespace(
        github_webhook_secret="test-only-secret-" * 3, public_base_url=origin))
    monkeypatch.setenv("DEPLOY_REPOSITORY", configure_github.REPOSITORY)
    calls = []

    def api(path, method="GET", data=None):
        calls.append((path, method))
        if path == f"repos/{configure_github.REPOSITORY}":
            return {"full_name": canonical}
        assert path.startswith(f"repos/{canonical}/hooks")
        if method == "PATCH":
            current.update(data)
        return [current] if "?" in path else current

    monkeypatch.setattr(configure_github, "api", api)
    configure_github.main()
    assert (f"repos/{canonical}/hooks/123", "PATCH") in calls
    assert current["config"]["url"] == origin + "/github/webhook"
    assert json.loads(state.read_text()) == {"id": 123, "repository": canonical}


@pytest.mark.parametrize("name", [None, "../escape", "owner/repo?bad", "owner/repo/extra"])
def test_invalid_canonical_repository_never_mutates_hook(monkeypatch, name):
    monkeypatch.setattr(configure_github.sys, "argv", ["configure_github.py", "--apply"])
    monkeypatch.setenv("DEPLOY_REPOSITORY", configure_github.REPOSITORY)
    monkeypatch.setattr(configure_github.Settings, "from_env", lambda: SimpleNamespace(
        github_webhook_secret="test-only-secret-" * 3, public_base_url="https://new.example"))
    calls = []

    def api(path, method="GET", data=None):
        calls.append(method)
        return {"full_name": name}

    monkeypatch.setattr(configure_github, "api", api)
    with pytest.raises(ValueError):
        configure_github.main()
    assert calls == ["GET"]
