"""Production transport must never fall back to another project's connection."""

import json
import subprocess
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest
from release import production_connection as remote


@pytest.fixture
def config(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("test key")
    key.chmod(0o600)
    pin = tmp_path / "known_hosts"
    pin.write_text("192.0.2.10 ssh-ed25519 test-public-key\n")
    pin.chmod(0o600)
    path = tmp_path / "connection.json"
    values = {
        "schema": 1,
        "host": "192.0.2.10",
        "user": "root",
        "identity_file": str(key),
        "known_hosts_file": str(pin),
        "host_public_key": "ssh-ed25519 test-public-key",
        "origin": "http://192.0.2.10",
        "username": "test@example.invalid",
        "password": "private-value",
        "registration_secret": "private-registration",
    }
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    monkeypatch.setattr(remote, "CONFIG_PATH", path)
    return path, values


def test_missing_config_never_runs_ssh(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "CONFIG_PATH", tmp_path / "missing")
    run = Mock()
    monkeypatch.setattr(remote.subprocess, "run", run)
    with pytest.raises(remote.ConnectionError, match="configuration"):
        remote.ssh_command()
    run.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("host", "generic-ecs"),
    ("origin", "http://192.0.2.11"),
    ("origin", "http://192.0.2.10:3030"),
    ("origin", "http://192.0.2.10/api"),
    ("user", "other"),
    ("identity_file", "/missing-key"),
    ("host_public_key", "ssh-ed25519 wrong-key"),
])
def test_invalid_target_is_rejected_without_network(config, monkeypatch, field, value):
    path, values = config
    values[field] = value
    path.write_text(json.dumps(values))
    run = Mock()
    monkeypatch.setattr(remote.subprocess, "run", run)
    with pytest.raises(remote.ConnectionError):
        remote.ssh_command()
    run.assert_not_called()


def test_private_config_permissions(config):
    config[0].chmod(0o644)
    with pytest.raises(remote.ConnectionError, match="permissions"):
        remote.load_config()


def test_ssh_ignores_generic_aliases_and_pins_exact_host(config, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, remote.IDENTITY_REPLY, ""))
    monkeypatch.setattr(remote.subprocess, "run", run)
    args = remote.ssh_command()
    assert args[-1] == "root@192.0.2.10"
    assert args[1:3] == ["-F", "/dev/null"]
    assert "StrictHostKeyChecking=yes" in args
    assert "IdentitiesOnly=yes" in args
    assert "GlobalKnownHostsFile=/dev/null" in args
    assert f"UserKnownHostsFile={config[1]['known_hosts_file']}" in args
    assert "3030" not in " ".join(args)


def test_wrong_application_identity_blocks_commands_and_login(config, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "other-project\n", ""))
    monkeypatch.setattr(remote.subprocess, "run", run)
    with pytest.raises(remote.ConnectionError, match="identity"):
        remote.ssh_command()
    with pytest.raises(remote.ConnectionError, match="identity"):
        remote.ProductionClient()
    assert run.call_count == 2


def test_recover_only_starts_the_pinned_tsp_unit(config, monkeypatch, capsys):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "ActiveState=activating\n", ""))
    monkeypatch.setattr(remote.subprocess, "run", run)
    assert remote.main(["recover"]) == 0
    args = run.call_args.args[0]
    assert args[-2] == "root@192.0.2.10"
    assert "StrictHostKeyChecking=yes" in args
    assert f"UserKnownHostsFile={config[1]['known_hosts_file']}" in args
    assert 'systemctl start --no-block "$unit"' in args[-1]
    assert args[-1].index("FragmentPath") < args[-1].index("systemctl start")
    assert args[-1].index("com.docker.compose.service") < args[-1].index("systemctl start")
    assert capsys.readouterr().out == "ActiveState=activating\n"


def test_recovery_status_is_read_only_and_recovery_refuses_target_override(config, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "ActiveState=failed\n", ""))
    monkeypatch.setattr(remote.subprocess, "run", run)
    assert remote.main(["recovery-status"]) == 0
    assert "systemctl start " not in run.call_args.args[0][-1]
    assert remote.main(["recover", "other.service"]) == 2
    run.assert_called_once()


def test_recovery_rejects_identity_failure_without_retry_or_fallback(config, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 42, "", "private stderr"))
    monkeypatch.setattr(remote.subprocess, "run", run)
    with pytest.raises(remote.ConnectionError, match="identity"):
        remote.recovery(start=True)
    run.assert_called_once()


@pytest.mark.parametrize("authenticated", [True, False])
def test_login_requires_confirmed_authenticated_session(config, monkeypatch, authenticated):
    monkeypatch.setattr(remote, "verify_identity", lambda: config[1])
    call = Mock(side_effect=[{"user": {"id": "test"}},
                            {"authenticated": authenticated},
                            {"status": "ok", "ready": True}])
    monkeypatch.setattr(remote.ProductionClient, "call", call)
    if authenticated:
        client = remote.ProductionClient()
        assert client.call("/health/ready")["ready"] is True
    else:
        with pytest.raises(remote.ConnectionError, match="authenticated"):
            remote.ProductionClient()
        assert call.call_count == 2


def test_tunnel_preserves_dedicated_key_and_fixed_forward(config, monkeypatch):
    values = config[1]
    values["tunnel_identity_file"] = values["identity_file"]
    monkeypatch.setattr(remote, "verify_identity", lambda: values)
    args = remote.tunnel_command()
    assert args[args.index("-i") + 1] == values["tunnel_identity_file"]
    assert args[args.index("-L") + 1] == "127.0.0.1:3039:127.0.0.1:3032"
    assert args[-1] == "root@192.0.2.10"
    assert "StrictHostKeyChecking=yes" in args


def test_tunnel_has_no_key_fallback(config, monkeypatch):
    monkeypatch.setattr(remote, "verify_identity", lambda: config[1])
    with pytest.raises(remote.ConnectionError, match="tunnel key"):
        remote.tunnel_command()


def test_cross_origin_redirect_does_not_send_credentials():
    handler = remote.SameOriginRedirect()
    request = urllib.request.Request("http://192.0.2.10/api/auth/login", data=b"secret")
    with pytest.raises(remote.ConnectionError, match="redirect"):
        handler.redirect_request(request, None, 302, "", {}, "http://192.0.2.11/login")
    assert handler.redirect_request(request, None, 302, "", {}, "http://192.0.2.10/login")


@pytest.mark.parametrize("path", ["//other/api", "http://other/api", "/../api", "/api#fragment"])
def test_http_path_cannot_override_origin(config, path):
    client = object.__new__(remote.ProductionClient)
    client.origin = config[1]["origin"]
    client.opener = Mock()
    with pytest.raises(remote.ConnectionError, match="path"):
        client.call(path)
    client.opener.open.assert_not_called()


@pytest.mark.parametrize("args", [
    ["ssh", "--host", "other", "true"],
    ["ssh", "-o", "StrictHostKeyChecking=no", "true"],
    ["ssh", "root@other", "true"],
    ["scp", "file", "root@other:/tmp/file"],
])
def test_cli_target_overrides_are_rejected_before_network(config, monkeypatch, args):
    run = Mock()
    monkeypatch.setattr(remote.subprocess, "run", run)
    assert remote.main(args) == 2
    run.assert_not_called()


def test_certificate_requires_explicit_address_before_creating_files(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "deploy/edge/issue-local-cert.sh"
    result = subprocess.run(["bash", str(script)], env={"PATH": "/usr/bin:/bin",
        "TSP_EDGE_PKI_DIR": str(tmp_path / "pki")}, capture_output=True, text=True)
    assert result.returncode == 2
    assert "TSP_EDGE_IP" in result.stderr
    assert not (tmp_path / "pki").exists()
