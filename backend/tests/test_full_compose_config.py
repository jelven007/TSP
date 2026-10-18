"""Resolve real Compose overlays and verify budget and mount boundaries."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI unavailable")
@pytest.mark.parametrize("live_market", [False, True])
def test_small_host_composes_all_roles_with_isolated_bounded_dependencies(tmp_path, live_market):
    env_file = tmp_path / "private.env"
    env_file.write_text("")
    values = {
        "COMPOSE_PROJECT_NAME": "tsp-compose-contract",
        "TSP_FULL_IMAGE": "test-runtime:fixed",
        "TSP_COMPUTE_IMAGE": "test-compute:fixed",
        "TSP_BROKER_IMAGE": "test-broker:fixed",
        "TSP_POSTGRES_IMAGE": "postgres:16",
        "TSP_NGINX_IMAGE": "nginx:alpine",
        "TSP_FULL_ENV_FILE": str(env_file),
        "TSP_PLATFORM_ENV_FILE": str(env_file),
        "TSP_CANDIDATE_POSTGRES_ENV_FILE": str(env_file),
        "TSP_CANDIDATE_NGINX_CONFIG": str(tmp_path / "nginx.conf"),
        **{key: str(tmp_path / key) for key in (
            "TSP_PLATFORM_DATA_DIR", "TSP_PLATFORM_CONFIG_DIR",
            "TSP_PUBLIC_MARKET_DIR", "TSP_COMPUTE_SOCKET_DIR",
        )},
    }
    env = {key: value for key, value in os.environ.items() if not key.startswith("TSP_")}
    env.update(values)
    budget = "acceptance.env" if live_market else "small-host.env"
    files = ["compose.yml", "compose.isolated.yml"]
    if live_market:
        files.append("compose.acceptance.yml")
    result = subprocess.run([
        "docker", "compose", "--env-file", str(ROOT / "deploy/full" / budget),
        *[arg for file in files for arg in ("-f", str(ROOT / "deploy/full" / file))],
        "config", "--format", "json",
    ], env=env, capture_output=True, text=True, check=True)
    config = json.loads(result.stdout)
    services = config["services"]
    assert len(services) == 10
    assert sum(int(s["mem_limit"]) for s in services.values()) + 1024**3 == (
        5376 if live_market else 5184
    ) * 1024**2
    for kind in ("research", "realtime", "notifications", "scheduled"):
        service = services[kind]
        assert service["environment"]["TSP_CELL_WORKER_CONCURRENCY"] == "1"
        assert service["command"][-1] == kind
        assert service["healthcheck"]["test"][-1] == "--check"
        assert service["restart"] == "unless-stopped"
    for role, service in services.items():
        assert service["logging"]["options"]["max-file"] == "3"
        if role not in {"postgres", "edge", "broker"}:
            assert service["read_only"] is True
            assert "probe" not in service["networks"]
            assert not any("docker.sock" in volume.get("source", "") for volume in service.get("volumes", []))
        assert ("market" in service.get("networks", {})) == (live_market and role == "platform")
    assert services["gateway"].get("volumes", []) == []
    assert services["gateway"].get("ports", []) == []
    assert services["api"].get("ports", []) == []
    assert services["broker"]["network_mode"] == "none"
    assert services["broker"]["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=32m"]
    for name, network in config["networks"].items():
        assert bool(network.get("internal")) == (name not in {"probe", "market"})
        assert not network.get("external")
