"""Accept the candidate SSE gateway through an isolated external Nginx boundary.

The script creates and removes its own Docker network, PostgreSQL server,
platform-marker fixture, candidate gateway, and Nginx proxy. Existing TSP
services, databases, containers, and ports are never reused.

Run from backend:
    uv run --frozen --no-sync python -m scripts.accept_external_sse
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.conninfo import make_conninfo

from app.multitenancy.events import append_event
from scripts.accept_phase2_step1 import _cluster, _login, _seed

DEFAULT_IMAGE = "tsp-p2-api:final"
DEFAULT_NGINX_IMAGE = "public.ecr.aws/docker/library/nginx:alpine"
DEFAULT_POSTGRES_IMAGE = "postgres:16"
POSTGRES_PASSWORD = "external-sse-disposable-password"
MARKET_TOKEN = "external-sse-market-token"
DEFAULT_CONCURRENT_CLIENTS = 100
MAX_CONCURRENT_CLIENTS = 500


def _command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        capture_output=True,
        text=True,
    )


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _command("docker", *args, check=check)


def _remove_container(name: str) -> None:
    _docker("rm", "-f", name, check=False)


def _remove_network(name: str) -> None:
    _docker("network", "rm", name, check=False)


def _published_port(name: str, container_port: int) -> int:
    value = _docker(
        "inspect",
        "--format",
        f'{{{{(index (index .NetworkSettings.Ports "{container_port}/tcp") 0).HostPort}}}}',
        name,
    ).stdout.strip()
    return int(value)


def _wait_postgres(dsn: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:
            error = exc
            time.sleep(0.25)
    raise RuntimeError(f"disposable PostgreSQL did not become ready: {error}")


def _wait_gateway(base_url: str, gateway: str, timeout: float = 45) -> dict:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            response = httpx.get(base_url + "/health/ready", timeout=2)
            last = {
                "status_code": response.status_code,
                "body": response.json(),
            }
            if response.status_code == 200 and last["body"].get("ready") is True:
                return last
        except Exception as exc:
            last = {"error": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.25)
    logs = _docker("logs", "--tail", "120", gateway, check=False)
    raise RuntimeError(
        "candidate gateway did not become ready: "
        f"{last}; logs={logs.stdout[-4000:]}{logs.stderr[-2000:]}"
    )


def _write_fixture(path: Path) -> None:
    path.write_text(
        f"""\
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = {MARKET_TOKEN!r}

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        if self.path != "/internal/cell-market/events":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.send_error(403)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sequence = 0
        try:
            while True:
                sequence += 1
                payload = json.dumps({{"sequence": sequence, "sent_at": time.time()}})
                self.wfile.write(
                    f"event: quotes_updated\\ndata: {{payload}}\\n\\n".encode()
                )
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            return

ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
""",
        encoding="utf-8",
    )


def _write_nginx(path: Path, gateway: str) -> None:
    path.write_text(
        f"""\
events {{}}
http {{
    access_log off;
    error_log /dev/stderr warn;

    upstream candidate_gateway {{
        server {gateway}:3018;
        keepalive 8;
    }}

    server {{
        listen 8080;
        server_name _;

        location = /health/live {{
            proxy_pass http://candidate_gateway;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}

        location = /health/ready {{
            proxy_pass http://candidate_gateway;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}

        location = /api/events {{
            proxy_pass http://candidate_gateway;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 30s;
        }}

        location = /api/events/ticket {{
            proxy_pass http://candidate_gateway;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_read_timeout 10s;
        }}

        location / {{
            return 404;
        }}
    }}
}}
""",
        encoding="utf-8",
    )


def _image_evidence(image: str) -> dict:
    value = json.loads(
        _docker("image", "inspect", image, "--format", "{{json .}}").stdout
    )
    return {
        "reference": image,
        "id": value["Id"],
        "architecture": value["Architecture"],
        "os": value["Os"],
        "repo_digests": value.get("RepoDigests") or [],
    }


def _container_dsn(dsn: str, postgres: str) -> str:
    return make_conninfo(dsn, host=postgres, port=5432)


def _issue_ticket(client: httpx.Client) -> str:
    response = client.post("/api/events/ticket")
    if response.status_code != 200:
        raise AssertionError(
            f"ticket endpoint returned {response.status_code}: {response.text}"
        )
    value = response.json()
    if value.get("expires_in") != 60 or not value.get("ticket", "").startswith("tss_"):
        raise AssertionError(f"invalid ticket response: {value}")
    return value["ticket"]


async def _issue_ticket_async(client: httpx.AsyncClient) -> tuple[str, float]:
    started = time.monotonic()
    response = await client.post("/api/events/ticket")
    latency_ms = (time.monotonic() - started) * 1000
    if response.status_code != 200:
        raise AssertionError(
            f"ticket endpoint returned {response.status_code}: {response.text}"
        )
    value = response.json()
    if value.get("expires_in") != 60 or not value.get("ticket", "").startswith("tss_"):
        raise AssertionError(f"invalid ticket response: {value}")
    return value["ticket"], latency_ms


def _frames(response: httpx.Response, timeout: float = 12):
    deadline = time.monotonic() + timeout
    frame: dict[str, str] = {}
    for line in response.iter_lines():
        if time.monotonic() >= deadline:
            raise TimeoutError("SSE frame deadline exceeded")
        if not line:
            if frame:
                yield frame
                frame = {}
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator:
            frame[field] = value.lstrip()
    raise RuntimeError("SSE stream ended before acceptance completed")


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * ratio) - 1)]


def _capacity_summary(
    results: list[dict],
    *,
    private_sequence: int,
    elapsed_seconds: float,
) -> dict:
    ticket = [float(item["ticket_latency_ms"]) for item in results]
    public = [float(item["public_latency_ms"]) for item in results]
    private = [float(item["private_latency_ms"]) for item in results]
    sequences = {int(item["private_sequence"]) for item in results}
    if sequences != {private_sequence}:
        raise AssertionError(
            f"concurrent clients observed unexpected private sequences: {sequences}"
        )
    return {
        "clients": len(results),
        "connected": len(results),
        "public_received": len(results),
        "private_received": len(results),
        "private_sequence": private_sequence,
        "ticket_latency_ms": {
            "p50": round(_percentile(ticket, 0.50), 3),
            "p95": round(_percentile(ticket, 0.95), 3),
            "max": round(max(ticket), 3),
        },
        "first_public_latency_ms": {
            "p50": round(_percentile(public, 0.50), 3),
            "p95": round(_percentile(public, 0.95), 3),
            "max": round(max(public), 3),
        },
        "private_fanout_latency_ms": {
            "p50": round(_percentile(private, 0.50), 3),
            "p95": round(_percentile(private, 0.95), 3),
            "max": round(max(private), 3),
        },
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _append(cluster, context, marker: str) -> int:
    with cluster.db.business_transaction(context, write=True) as conn:
        return int(
            append_event(
                conn,
                context.tenant_id,
                "strategy_results_updated",
                {"marker": marker},
            )
        )


def _stream_acceptance(
    base_url: str,
    token: str,
    cluster,
    principal: dict,
) -> dict:
    context = cluster.identity.context(
        principal["account"],
        principal["tenant_id"],
        "external-sse-acceptance",
    )
    baseline_marker = "baseline-" + uuid.uuid4().hex
    live_marker = "live-" + uuid.uuid4().hex
    replay_marker = "replay-" + uuid.uuid4().hex
    baseline_sequence = _append(cluster, context, baseline_marker)

    headers = {
        "Cookie": f"tf_session={token}",
        "Origin": base_url,
    }
    captured: list[dict] = []
    with httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(15, read=15),
    ) as client:
        ticket = _issue_ticket(client)
        started = time.monotonic()
        with client.stream("GET", "/api/events", params={"ticket": ticket}) as response:
            if response.status_code != 200:
                raise AssertionError(
                    f"first stream returned {response.status_code}: {response.read()!r}"
                )
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                raise AssertionError("Nginx did not expose an event-stream response")

            replay = client.get("/api/events", params={"ticket": ticket})
            if replay.status_code != 401:
                raise AssertionError(
                    f"consumed ticket replay returned {replay.status_code}"
                )

            public_latency_ms: float | None = None
            live_sequence: int | None = None
            for frame in _frames(response):
                captured.append(frame)
                if frame.get("event") == "quotes_updated" and live_sequence is None:
                    public_latency_ms = (time.monotonic() - started) * 1000
                    live_sequence = _append(cluster, context, live_marker)
                if frame.get("event") == "strategy_results_updated":
                    payload = json.loads(frame.get("data", "{}"))
                    if payload.get("marker") == baseline_marker:
                        raise AssertionError("a pre-subscription event leaked without replay cursor")
                    if payload.get("marker") == live_marker:
                        if int(frame["id"]) != live_sequence:
                            raise AssertionError("live private event sequence changed")
                        break
            else:
                raise AssertionError("live private event was not received")

            if public_latency_ms is None or public_latency_ms > 4_000:
                raise AssertionError(
                    f"Nginx-buffered or delayed public marker: {public_latency_ms}"
                )
            accel_buffering = response.headers.get("x-accel-buffering")

        replay_sequence = _append(cluster, context, replay_marker)
        ticket = _issue_ticket(client)
        replay_frames: list[dict] = []
        with client.stream(
            "GET",
            "/api/events",
            params={"ticket": ticket},
            headers={"Last-Event-ID": str(live_sequence)},
        ) as response:
            if response.status_code != 200:
                raise AssertionError(
                    f"reconnect returned {response.status_code}: {response.read()!r}"
                )
            for frame in _frames(response):
                replay_frames.append(frame)
                if frame.get("event") != "strategy_results_updated":
                    continue
                payload = json.loads(frame.get("data", "{}"))
                if payload.get("marker") == replay_marker:
                    if int(frame["id"]) != replay_sequence:
                        raise AssertionError("replayed event sequence changed")
                    break
            else:
                raise AssertionError("Last-Event-ID replay event was not received")

    return {
        "ticket_single_use": True,
        "public_marker": {
            "received": True,
            "latency_ms": round(public_latency_ms, 3),
            "x_accel_buffering": accel_buffering,
        },
        "private_event": {
            "baseline_sequence": baseline_sequence,
            "live_sequence": live_sequence,
            "received": True,
        },
        "reconnect": {
            "last_event_id": live_sequence,
            "replayed_sequence": replay_sequence,
            "received": True,
        },
        "captured_event_kinds": sorted(
            {frame.get("event") for frame in captured + replay_frames if frame.get("event")}
        ),
    }


async def _concurrent_fanout_async(
    base_url: str,
    token: str,
    cluster,
    principal: dict,
    *,
    clients: int,
) -> dict:
    context = cluster.identity.context(
        principal["account"],
        principal["tenant_id"],
        "external-sse-concurrent-acceptance",
    )
    marker = "fanout-" + uuid.uuid4().hex
    all_public_ready = asyncio.Event()
    public_ready = 0
    private_sent_at: float | None = None
    started = time.monotonic()

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Cookie": f"tf_session={token}",
            "Origin": base_url,
        },
        limits=httpx.Limits(
            max_connections=clients + 16,
            max_keepalive_connections=min(clients, 32),
        ),
        timeout=httpx.Timeout(35, connect=10, read=35),
    ) as client:

        async def subscriber(client_id: int) -> dict:
            nonlocal public_ready
            ticket, ticket_latency_ms = await _issue_ticket_async(client)
            stream_started = time.monotonic()
            public_latency_ms: float | None = None
            frame: dict[str, str] = {}
            async with client.stream(
                "GET",
                "/api/events",
                params={"ticket": ticket},
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise AssertionError(
                        f"client {client_id} stream returned "
                        f"{response.status_code}: {body!r}"
                    )
                if not response.headers.get("content-type", "").startswith(
                    "text/event-stream"
                ):
                    raise AssertionError(
                        f"client {client_id} did not receive an event stream"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        if not frame:
                            continue
                        event = frame
                        frame = {}
                        if (
                            event.get("event") == "quotes_updated"
                            and public_latency_ms is None
                        ):
                            public_latency_ms = (
                                time.monotonic() - stream_started
                            ) * 1000
                            public_ready += 1
                            if public_ready == clients:
                                all_public_ready.set()
                        if event.get("event") != "strategy_results_updated":
                            continue
                        payload = json.loads(event.get("data", "{}"))
                        if payload.get("marker") != marker:
                            continue
                        if private_sent_at is None:
                            raise AssertionError(
                                f"client {client_id} received private event "
                                "before publication"
                            )
                        if public_latency_ms is None:
                            raise AssertionError(
                                f"client {client_id} missed the public marker"
                            )
                        return {
                            "ticket_latency_ms": ticket_latency_ms,
                            "public_latency_ms": public_latency_ms,
                            "private_latency_ms": (
                                time.monotonic() - private_sent_at
                            ) * 1000,
                            "private_sequence": int(event["id"]),
                        }
                    if line.startswith(":"):
                        continue
                    field, separator, value = line.partition(":")
                    if separator:
                        frame[field] = value.lstrip()
            raise RuntimeError(
                f"client {client_id} stream ended before fanout completed"
            )

        tasks = [
            asyncio.create_task(subscriber(client_id))
            for client_id in range(clients)
        ]
        try:
            async def wait_until_public_ready() -> None:
                while not all_public_ready.is_set():
                    for task in tasks:
                        if task.done() and (error := task.exception()) is not None:
                            raise error
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(wait_until_public_ready(), timeout=30)
            private_sent_at = time.monotonic()
            private_sequence = _append(cluster, context, marker)
            results = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=20,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return _capacity_summary(
        results,
        private_sequence=private_sequence,
        elapsed_seconds=time.monotonic() - started,
    )


def _concurrent_fanout_acceptance(
    base_url: str,
    token: str,
    cluster,
    principal: dict,
    *,
    clients: int,
) -> dict:
    if not 1 <= clients <= MAX_CONCURRENT_CLIENTS:
        raise ValueError(
            f"concurrent clients must be between 1 and {MAX_CONCURRENT_CLIENTS}"
        )
    return asyncio.run(
        _concurrent_fanout_async(
            base_url,
            token,
            cluster,
            principal,
            clients=clients,
        )
    )


def _manifest_evidence(gateway: str) -> dict:
    result = _docker(
        "exec",
        gateway,
        "/app/.venv/bin/python",
        "/app/build_manifest.py",
        "verify",
        "--root",
        "/app",
        "--manifest",
        "/app/build-manifest.json",
    )
    manifest = _docker(
        "exec",
        gateway,
        "/app/.venv/bin/python",
        "-c",
        (
            "import hashlib,json,pathlib;"
            "p=pathlib.Path('/app/build-manifest.json');"
            "v=json.loads(p.read_text());"
            "print(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
            "'version':v.get('version'),'commit':v.get('commit'),"
            "'files':len(v.get('files',[]))}))"
        ),
    )
    return {
        "verified": result.returncode == 0,
        **json.loads(manifest.stdout),
    }


def run(
    *,
    image: str,
    nginx_image: str,
    postgres_image: str,
    output: Path,
    concurrent_clients: int = DEFAULT_CONCURRENT_CLIENTS,
) -> dict:
    if not 1 <= concurrent_clients <= MAX_CONCURRENT_CLIENTS:
        raise ValueError(
            "concurrent clients must be between "
            f"1 and {MAX_CONCURRENT_CLIENTS}"
        )
    suffix = uuid.uuid4().hex[:10]
    network = f"tsp-sse-{suffix}"
    postgres = f"tsp-sse-postgres-{suffix}"
    market = f"tsp-sse-market-{suffix}"
    gateway = f"tsp-sse-gateway-{suffix}"
    nginx = f"tsp-sse-nginx-{suffix}"
    stream_secret = "external-sse-" + uuid.uuid4().hex + uuid.uuid4().hex
    started = time.time()
    report: dict[str, Any] = {
        "status": "failed",
        "scope": "local_isolated_candidate_image_through_external_nginx_boundary",
        "limits": [
            "HTTP loopback boundary only; no public DNS, WAN, certificate, or TLS termination",
            "disposable PostgreSQL and deterministic platform marker fixture",
            (
                f"{concurrent_clients}-client local burst fanout only; "
                "not a sustained production capacity test"
            ),
        ],
        "artifact": _image_evidence(image),
        "git": {
            "head": _command(
                "git", "rev-parse", "HEAD", check=True,
            ).stdout.strip(),
            "dirty": bool(
                _command("git", "status", "--short", check=True).stdout
            ),
        },
        "resources": {
            "network": network,
            "containers": [postgres, market, gateway, nginx],
        },
    }
    try:
        with tempfile.TemporaryDirectory(prefix="tsp-external-sse-") as temporary:
            temp = Path(temporary)
            fixture_path = temp / "market_fixture.py"
            nginx_path = temp / "nginx.conf"
            _write_fixture(fixture_path)
            _write_nginx(nginx_path, gateway)

            with ExitStack() as outer:
                _docker("network", "create", network)
                outer.callback(_remove_network, network)
                _docker(
                    "run",
                    "-d",
                    "--name",
                    postgres,
                    "--network",
                    network,
                    "-p",
                    "127.0.0.1::5432",
                    "-e",
                    f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
                    "-e",
                    "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                    postgres_image,
                )
                outer.callback(_remove_container, postgres)
                postgres_port = _published_port(postgres, 5432)
                root_dsn = (
                    f"postgresql://postgres:{POSTGRES_PASSWORD}"
                    f"@127.0.0.1:{postgres_port}/postgres"
                )
                _wait_postgres(root_dsn)

                with _cluster(
                    root_dsn,
                    8,
                    name_prefix="tsp_sse_",
                    base_url="http://localhost",
                ) as cluster:
                    principal = _login(cluster, _seed(cluster, 1)).pop("principals")[0]
                    control_dsn = _container_dsn(
                        cluster.settings.control_dsn.get_secret_value(),
                        postgres,
                    )
                    data_dsn = _container_dsn(
                        cluster.settings.data_dsn.get_secret_value(),
                        postgres,
                    )

                    with ExitStack() as services:
                        _docker(
                            "run",
                            "-d",
                            "--name",
                            market,
                            "--network",
                            network,
                            "--read-only",
                            "--tmpfs",
                            "/tmp:rw,nosuid,nodev,size=32m",
                            "-v",
                            f"{fixture_path}:/fixture.py:ro",
                            image,
                            "/app/.venv/bin/python",
                            "/fixture.py",
                        )
                        services.callback(_remove_container, market)

                        environment = {
                            "SERVICE_ROLE": "gateway",
                            "MULTITENANCY_ENABLED": "true",
                            "TSP_CELL_ID": "full",
                            "TSP_CELL_SCHEMA_VERSION": "6",
                            "TSP_CELL_POOL_SIZE": "8",
                            "TSP_CELL_CONTROL_DSN": control_dsn,
                            "TSP_CELL_DATA_DSN": data_dsn,
                            "TSP_CELL_MARKET_URL": f"http://{market}:8000",
                            "TSP_CELL_MARKET_TOKEN": MARKET_TOKEN,
                            "TSP_CELL_STREAM_TICKET_SECRET": stream_secret,
                            "TICKFLOW_ENV_FILE": "/dev/null",
                            "NUMBA_CACHE_DIR": "/tmp/numba",
                            "PYTHONDONTWRITEBYTECODE": "1",
                        }
                        gateway_args = [
                            "run",
                            "-d",
                            "--name",
                            gateway,
                            "--network",
                            network,
                            "--read-only",
                            "--tmpfs",
                            "/tmp:rw,nosuid,nodev,size=128m",
                        ]
                        for key, value in environment.items():
                            gateway_args.extend(("-e", f"{key}={value}"))
                        gateway_args.extend(
                            (
                                image,
                                "/app/.venv/bin/python",
                                "-m",
                                "uvicorn",
                                "app.main:app",
                                "--host",
                                "0.0.0.0",
                                "--port",
                                "3018",
                                "--workers",
                                "1",
                                "--proxy-headers",
                                "--forwarded-allow-ips",
                                "*",
                            )
                        )
                        _docker(*gateway_args)
                        services.callback(_remove_container, gateway)

                        _docker(
                            "run",
                            "-d",
                            "--name",
                            nginx,
                            "--network",
                            network,
                            "-p",
                            "127.0.0.1::8080",
                            "-v",
                            f"{nginx_path}:/etc/nginx/nginx.conf:ro",
                            nginx_image,
                        )
                        services.callback(_remove_container, nginx)
                        nginx_port = _published_port(nginx, 8080)
                        base_url = f"http://127.0.0.1:{nginx_port}"

                        readiness = _wait_gateway(base_url, gateway)
                        report.update(
                            {
                                "boundary": {
                                    "url": base_url,
                                    "nginx_image": nginx_image,
                                    "proxy_buffering": False,
                                },
                                "readiness": readiness,
                                "manifest": _manifest_evidence(gateway),
                                "sse": _stream_acceptance(
                                    base_url,
                                    principal["token"],
                                    cluster,
                                    principal,
                                ),
                                "concurrent_fanout": _concurrent_fanout_acceptance(
                                    base_url,
                                    principal["token"],
                                    cluster,
                                    principal,
                                    clients=concurrent_clients,
                                ),
                                "status": "passed",
                            }
                        )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["elapsed_seconds"] = round(time.time() - started, 3)
        report["report_sha256"] = hashlib.sha256(
            json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--nginx-image", default=DEFAULT_NGINX_IMAGE)
    parser.add_argument("--postgres-image", default=DEFAULT_POSTGRES_IMAGE)
    parser.add_argument(
        "--clients",
        type=int,
        default=DEFAULT_CONCURRENT_CLIENTS,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "../reports/p2-remaining-20261017/external-sse-candidate.json"
        ),
    )
    args = parser.parse_args()
    result = run(
        image=args.image,
        nginx_image=args.nginx_image,
        postgres_image=args.postgres_image,
        output=args.output,
        concurrent_clients=args.clients,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
