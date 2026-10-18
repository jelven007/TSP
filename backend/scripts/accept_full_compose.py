"""Exercise all seven real roles in a disposable Compose topology.

Uses a small deterministic market fixture, two private tenants, isolated
PostgreSQL, a real Docker broker and Nginx. Does not certify production load,
market freshness, public TLS or a full trading day.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from app.multitenancy.documents import TenantDocuments
from app.multitenancy.research import ResearchJobs
from scripts.accept_external_sse import (
    _container_dsn,
    _docker,
    _image_evidence,
    _published_port,
    _stream_acceptance,
    _wait_postgres,
)
from scripts.accept_phase2_step1 import _cluster, _login, _seed

ROOT = Path(__file__).resolve().parents[2]
ROLES = ("platform", "gateway", "api", "research", "realtime", "notifications", "scheduled")


def write_env(path: Path, values: dict) -> None:
    # No shell interpolation; runtime values are passed through Compose env_file.
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        for key, value in values.items():
            if "\n" in str(value) or "\r" in str(value):
                raise ValueError("multiline environment value")
            stream.write(f"{key}={value}\n")


def wait_for(check, *, timeout=180):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(.5)
    raise TimeoutError(f"candidate condition timed out ({type(last).__name__})")


def ready(url: str) -> dict | None:
    response = httpx.get(url + "/health/ready", timeout=8)
    return response.json() if response.status_code == 200 and response.json().get("ready") else None


def nginx_config() -> str:
    return """events {}
http {
    access_log off;
    server {
        listen 8080;
        location = /api/events {
            proxy_pass http://tsp-full-gateway:3018;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $http_host;
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 75s;
        }
        location = /api/events/ticket {
            proxy_pass http://tsp-full-gateway:3018;
            proxy_set_header Host $http_host;
        }
        location / {
            proxy_pass http://tsp-full-api:3018;
            proxy_set_header Host $http_host;
        }
    }
}
"""


def run(*, build_dir: Path, output: Path, work_dir: Path) -> dict:
    built = json.loads((build_dir / "build.json").read_text())
    runtime = built["artifacts"]["runtime"]["id"]
    compute = built["artifacts"]["compute"]["id"]
    broker = built["artifacts"]["broker"]["id"]
    project = "tsp-candidate-" + uuid.uuid4().hex[:10]
    report = {
        "status": "failed", "project": project, "build": built,
        "scope": "isolated_compose_small_fixture",
        "limits": ["two tenants; not sustained capacity", "historical fixture; not market freshness",
                   "loopback HTTP; not public TLS", "no production cutover"],
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tsp-compose-", dir=work_dir) as temp:
        root = Path(temp).resolve()
        for name in ("platform", "public", "config", "socket"):
            (root / name).mkdir(mode=0o777)
            (root / name).chmod(0o777)
        # Keep the fixture generator shared with the existing compute acceptance.
        spec = importlib.util.spec_from_file_location("compute_fixture", ROOT / "scripts/verify_full_compute.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        _docker("run", "--rm", "--network", "none", "--user", "0:0",
                "-v", f"{root / 'platform'}:/fixture", "--entrypoint", "/app/.venv/bin/python",
                runtime, "-c", fixture.FIXTURE)
        _docker("run", "--rm", "--network", "none", "--user", "0:0",
                "-v", f"{root}:/candidate", "--entrypoint", "/bin/chown",
                runtime, "-R", "10001:10001", "/candidate/platform", "/candidate/public", "/candidate/config")
        password = secrets.token_hex(24)
        market_token, admin_token, stream_secret = (secrets.token_hex(32) for _ in range(3))
        write_env(root / "postgres.env", {"POSTGRES_PASSWORD": password})
        write_env(root / "platform.env", {
            "TSP_PLATFORM_MARKET_TOKEN": market_token, "TSP_PLATFORM_ADMIN_TOKEN": admin_token,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        (root / "runtime.env").touch(mode=0o600)
        (root / "nginx.conf").write_text(nginx_config())
        env = {
            **{key: value for key, value in os.environ.items() if not key.startswith("TSP_")},
            "COMPOSE_PROJECT_NAME": project,
            "TSP_FULL_IMAGE": runtime, "TSP_COMPUTE_IMAGE": compute, "TSP_BROKER_IMAGE": broker,
            "TSP_FULL_ENV_FILE": str(root / "runtime.env"),
            "TSP_PLATFORM_ENV_FILE": str(root / "platform.env"),
            "TSP_CANDIDATE_POSTGRES_ENV_FILE": str(root / "postgres.env"),
            "TSP_CANDIDATE_NGINX_CONFIG": str(root / "nginx.conf"),
            "TSP_PLATFORM_DATA_DIR": str(root / "platform"),
            "TSP_PUBLIC_MARKET_DIR": str(root / "public"),
            "TSP_PLATFORM_CONFIG_DIR": str(root / "config"),
            "TSP_COMPUTE_SOCKET_DIR": str(root / "socket"),
            "TSP_FULL_API_PORT": "0", "TSP_FULL_GATEWAY_PORT": "0",
            "TSP_POSTGRES_IMAGE": _image_evidence("postgres:16")["id"],
            "TSP_NGINX_IMAGE": _image_evidence("public.ecr.aws/docker/library/nginx:alpine")["id"],
        }
        # Use the exact deployment files bound to the image build.
        deployment = build_dir / "source/deploy/full"

        def compose(*args, check=True):
            return subprocess.run([
                "docker", "compose", "--env-file", str(deployment / "small-host.env"),
                "-p", project, "-f", str(deployment / "compose.yml"),
                "-f", str(deployment / "compose.isolated.yml"), *args,
            ], env=env, capture_output=True, text=True, check=check)

        def container(role):
            return compose("ps", "--all", "-q", role).stdout.strip()

        def inspect(role):
            return json.loads(_docker("inspect", container(role)).stdout)[0]

        def services_ready():
            values = {role: inspect(role) for role in (*ROLES, "broker")}
            return values if all(v["State"].get("Health", {}).get("Status") == "healthy"
                                 for v in values.values()) else None

        def gateway_status():
            response = _docker(
                "exec", container("gateway"), "/app/.venv/bin/python", "-c",
                "import urllib.request; "
                "print(urllib.request.urlopen('http://127.0.0.1:3018/health/ready', timeout=8).read().decode())",
                check=False,
            )
            return json.loads(response.stdout) if response.returncode == 0 else {}

        try:
            config = json.loads(compose("config", "--format", "json").stdout)
            total = sum(int(s.get("mem_limit", 0)) for s in config["services"].values()) + 1024**3
            report["budget"] = {"container_memory_limit_bytes": total, "compute_capacity": 1}
            docker = json.loads(_docker("info", "--format", "{{json .}}").stdout)
            if total + 2 * 1024**3 > docker["MemTotal"]:
                raise RuntimeError("Docker memory cannot fit candidate budget plus 2 GiB reserve")
            print("Starting disposable PostgreSQL", flush=True)
            compose("up", "-d", "postgres")
            pg = container("postgres")
            dsn = f"postgresql://postgres:{password}@127.0.0.1:{_published_port(pg, 5432)}/postgres"
            _wait_postgres(dsn)
            with _cluster(dsn, 2, name_prefix="tsp_compose_", base_url="http://localhost") as cluster:
                principals = _login(cluster, _seed(cluster, 2))["principals"]
                # Replace only this newly-created empty environment file.
                (root / "runtime.env").unlink()
                write_env(root / "runtime.env", {
                    "TSP_CELL_ID": "full", "TSP_CELL_SCHEMA_VERSION": 6,
                    "TSP_CELL_CONTROL_DSN": _container_dsn(cluster.settings.control_dsn.get_secret_value(), "postgres"),
                    "TSP_CELL_DATA_DSN": _container_dsn(cluster.settings.data_dsn.get_secret_value(), "postgres"),
                    "TSP_CELL_MARKET_URL": "http://platform:3018",
                    "TSP_CELL_MARKET_TOKEN": market_token, "TSP_PLATFORM_ADMIN_TOKEN": admin_token,
                    "TSP_CELL_STREAM_TICKET_SECRET": stream_secret, "PYTHONDONTWRITEBYTECODE": "1",
                })
                try:
                    print("Starting full Compose topology", flush=True)
                    compose("up", "-d")
                    values = wait_for(services_ready, timeout=240)
                    print("All roles healthy; verifying HTTP isolation and SSE", flush=True)
                    base = f"http://127.0.0.1:{_published_port(container('edge'), 8080)}"
                    report["readiness"] = wait_for(lambda: ready(base))
                    report["roles"] = {role: {
                        "image": value["Image"], "healthy": True,
                        "memory_limit": value["HostConfig"]["Memory"],
                        "read_only": value["HostConfig"]["ReadonlyRootfs"],
                    } for role, value in values.items()}
                    assert all(value["Image"] == runtime for role, value in values.items() if role in ROLES)
                    assert "text/html" in httpx.get(base + "/", timeout=10).headers["content-type"]
                    clients = [httpx.Client(
                        base_url=base, headers={"Cookie": f"tf_session={p['token']}", "Origin": base},
                        timeout=15,
                    ) for p in principals]
                    try:
                        added = clients[0].post("/api/watchlist", json={"symbol": "600000.SH"})
                        assert added.status_code == 200, added.text
                        assert "600000.SH" in clients[0].get("/api/watchlist").text
                        assert "600000.SH" not in clients[1].get("/api/watchlist").text
                        report["tenant_isolation"] = True
                        report["sse"] = _stream_acceptance(
                            base, principals[0]["token"], cluster, principals[0],
                            wait_for_private_subscription=lambda: wait_for(lambda:
                                gateway_status().get("checks", {}).get("private_events", {}).get("subscriptions") == 1),
                        )
                        report["sse"]["limits"] = ["live public quote markers require trading-day acceptance"]

                        # Verify Docker restart policy, supervisor child reaping and retained business rows.
                        print("Verifying API, research and broker restart recovery", flush=True)
                        restarts = {}
                        for role in ("api", "research", "broker"):
                            before = inspect(role)["RestartCount"]
                            if role == "research":
                                _docker("exec", container(role), "/app/.venv/bin/python", "-c",
                                        "import json,os,signal; "
                                        "p=json.load(open('/tmp/tsp-worker/owner.json'))['pid']; "
                                        "os.kill(p, signal.SIGKILL)")
                            else:
                                # Docker's kill/stop API marks a manual stop and
                                # suppresses unless-stopped. Exit from inside the
                                # container to exercise the restart policy.
                                _docker("exec", container(role), "/app/.venv/bin/python", "-c",
                                        "import os,signal; os.kill(1,signal.SIGTERM)", check=False)
                            wait_for(lambda role=role, before=before: inspect(role)["RestartCount"] > before)
                            wait_for(services_ready)
                            wait_for(lambda: ready(base))
                            restarts[role] = inspect(role)["RestartCount"] - before
                        assert "600000.SH" in clients[0].get("/api/watchlist").text
                        report["restart_recovery"] = restarts
                        report["restart_triggers"] = {
                            "api": "SIGTERM to PID 1 from inside container",
                            "research": "SIGKILL to supervised dispatch child",
                            "broker": "SIGTERM to PID 1 from inside container",
                        }

                        # A suspended dispatch cannot be kept healthy by process existence.
                        before = inspect("research")["RestartCount"]
                        _docker("exec", container("research"), "/app/.venv/bin/python", "-c",
                                "import json,os,signal,pathlib,time; "
                                "p=pathlib.Path('/tmp/tsp-worker/progress.json'); "
                                "v=json.loads(p.read_text()); os.kill(v['pid'],signal.SIGSTOP); "
                                "v['at']=time.monotonic()-121; p.write_text(json.dumps(v))")
                        wait_for(lambda: inspect("research")["RestartCount"] > before)
                        wait_for(services_ready)
                        report["stalled_dispatch_recovery"] = "suspended child with forced expired timestamp"

                        # Disconnect only the candidate gateway from its database network.
                        print("Verifying database network recovery", flush=True)
                        gateway = container("gateway")
                        pg_network = project + "_pg"
                        _docker("network", "disconnect", pg_network, gateway)
                        try:
                            wait_for(lambda: not gateway_status().get("ready"), timeout=60)
                        finally:
                            _docker("network", "connect", pg_network, gateway)
                        wait_for(lambda: gateway_status().get("ready"))
                        report["database_network_recovery"] = True

                        print("Verifying queued compute after recovery", flush=True)
                        principal = principals[0]
                        context = cluster.identity.context(principal["account"], principal["tenant_id"], "compute-check")
                        jobs = ResearchJobs(TenantDocuments(cluster.db, context))
                        row = jobs.submit({
                            "kind": "describe", "tenant_id": principal["tenant_id"],
                            "strategy_id": "custom_sandbox",
                            "sources": [{"id": "custom_sandbox", "source": "custom", "code": fixture.CODE}],
                            "overrides": {}, "factors": [], "signals": [],
                        }, force=True)
                        terminal = wait_for(lambda: (
                            result if (result := jobs.get(row["id"]))["state"] not in
                            {"queued", "running", "cancelling"} else None
                        ))
                        assert terminal["state"] == "succeeded", terminal["error"]
                        report["research_after_recovery"] = "succeeded"
                    finally:
                        for client in clients:
                            client.close()
                    report["status"] = "passed"
                finally:
                    # Stop dependents before disposable database context cleanup.
                    compose("stop", "-t", "30", *ROLES, "broker", "edge", check=False)
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            # Retain startup evidence without recording generated credentials.
            output.parent.mkdir(parents=True, exist_ok=True)
            logs = compose("logs", "--no-color", "--tail", "80", check=False).stdout
            if isinstance(exc, subprocess.CalledProcessError):
                logs = (exc.stderr or "") + "\n" + logs
            for secret in (password, market_token, admin_token, stream_secret):
                logs = logs.replace(secret, "[redacted]")
            logs = re.sub(r"([?&]ticket=)[^ \t\r\n\"&]+", r"\1[redacted]", logs)
            log_path = output.parent / f"{project}.log"
            log_path.touch(mode=0o600)
            log_path.write_text(logs)
            raise
        finally:
            result = compose("down", "--volumes", "--remove-orphans", check=False)
            report["cleanup_completed"] = result.returncode == 0
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(build_dir=args.build_dir.resolve(), output=args.output.resolve(),
                         work_dir=args.work_dir.resolve()), ensure_ascii=False, indent=2))
