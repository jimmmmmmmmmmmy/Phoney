"""The local unlock command exposes one short-lived grant, never server keys."""

import pytest

from agent_registry import AgentRegistry, RegistryError
from scripts.operator_access import main


def write_env(tmp_path, enabled="true"):
    config = tmp_path / "private.env"
    config.write_text(f"AGENT_MANAGEMENT_ENABLED={enabled}\nWORKSPACE_STORAGE_DIR={tmp_path / 'workspace'}\n"
                      "OPERATOR_ADMIN_TOKEN=never-display-this-admin-secret\nELEVENLABS_API_KEY=never-display-provider-key\n")
    return config


def test_local_cli_grant_is_redeemable_and_never_prints_credentials(tmp_path, capsys):
    env = write_env(tmp_path)
    main(["--env-file", str(env)])
    output = capsys.readouterr().out
    assert "never-display" not in output
    code = output.strip().splitlines()[-1]
    registry = AgentRegistry(str(tmp_path / "workspace"))
    token = registry.consume_grant(code)
    assert registry.authorized(token)
    main(["--env-file", str(env), "--revoke"])
    assert not registry.authorized(token)


def test_cli_refuses_disabled_feature_but_revocation_remains_available(tmp_path, capsys):
    env = write_env(tmp_path, "false")
    with pytest.raises(RegistryError, match="Enable AGENT_MANAGEMENT_ENABLED"):
        main(["--env-file", str(env)])
    assert capsys.readouterr().out == ""
    main(["--env-file", str(env), "--revoke"])
    assert "revoked" in capsys.readouterr().out
