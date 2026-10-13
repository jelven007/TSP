"""Optional smoke test of the built, isolated Cell image against real PostgreSQL."""

import os
import subprocess
import time

import pytest

pytest.importorskip("psycopg")
from psycopg.conninfo import make_conninfo
from test_cell_isolation import PASSWORD, cluster  # noqa: F401 -- shared PostgreSQL fixture

from app.cell.admin import provision_tenant, sync_catalog


def test_cell_image(cluster):  # noqa: F811 -- pytest fixture injection
    image = os.environ.get("TSP_TEST_CELL_IMAGE")
    pg_container = os.environ.get("TSP_TEST_POSTGRES_CONTAINER")
    if not image or not pg_container:
        pytest.skip("set TSP_TEST_CELL_IMAGE and TSP_TEST_POSTGRES_CONTAINER for container smoke")
    settings = cluster["settings"][0]
    provision_tenant(cluster["control"], cluster["data"]["a"], "container-smoke", "Smoke", "a", cluster["users"]["owner"])
    env = os.environ.copy()
    env["TSP_CELL_ID"] = "a"
    for variable, dsn in (
        ("TSP_CELL_CONTROL_DSN", settings.control_dsn),
        ("TSP_CELL_DATA_DSN", settings.data_dsn),
    ):
        env[variable] = make_conninfo(dsn.get_secret_value(), host="127.0.0.1", port="5432")
    docker = ["docker"]
    if os.environ.get("TSP_TEST_DOCKER_CONTEXT"):
        docker += ["--context", os.environ["TSP_TEST_DOCKER_CONTEXT"]]
    container = None
    try:
        result = subprocess.run(
            [*docker,
                "run", "-d", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--network=container:" + pg_container,
                "-e", "TSP_CELL_ID", "-e", "TSP_CELL_CONTROL_DSN", "-e", "TSP_CELL_DATA_DSN",
                image,
            ], env=env, capture_output=True, text=True, check=True,
        )
        container = result.stdout.strip()
        probe = (
            "import urllib.request; "
            "urllib.request.urlopen('http://127.0.0.1:3018/health/ready', timeout=2)"
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ready = subprocess.run(
                [*docker, "exec", container, "python", "-c", probe], capture_output=True, text=True
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Cell image did not become ready")
        script = """
import importlib.util
import json
import os
import urllib.request

assert os.getuid() == 10001
assert importlib.util.find_spec('app.main') is None
assert importlib.util.find_spec('app.plugins') is None
assert importlib.util.find_spec('app.services') is None
def call(path, body=None, token=None):
    headers = {'Content-Type': 'application/json', 'X-Routing-Epoch': '1'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request('http://127.0.0.1:3018' + path,
        data=json.dumps(body).encode() if body else None, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.headers['Cache-Control'] == 'no-store'
        return json.load(response)
session = call('/api/auth/login', {'email': 'owner@example.test', 'password': 'test-cell-password'})
token = session['token']
directory = call('/api/tenants', token=token)
assert any(t['id'] == 'container-smoke' for t in directory['tenants'])
call('/api/tenants/container-smoke/watchlist',
     {'symbols': ['000001.SZ'], 'note': 'container-private'}, token)
assert call('/api/tenants/container-smoke/watchlist', token=token)['symbols'][0]['note'] == 'container-private'
assert call('/api/tenants/container-smoke/strategies', token=token)['strategies'] == []
assert call('/api/tenants/container-smoke/alerts', token=token)['alerts'] == []
account = call('/api/tenants/container-smoke/paper/accounts', {'name': 'Smoke', 'initial_cash': 10000}, token)['account']
assert float(call('/api/tenants/container-smoke/paper/accounts/' + account['id'], token=token)['account']['cash']) == 10000
with urllib.request.urlopen('http://127.0.0.1:3018/') as response:
    assert b'<div id="root">' in response.read()
print('Cell container health, login, directory, watchlist and isolation: passed')
"""
        smoke = subprocess.run(
            [*docker, "exec", container, "python", "-c", script],
            capture_output=True, text=True, check=True,
        )
        assert "passed" in smoke.stdout
    finally:
        if container:
            subprocess.run([*docker, "rm", "-f", container], capture_output=True, check=True)


def test_worker_image_executes_and_settles(cluster):  # noqa: F811
    import json
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient
    from test_cell_business import Market

    from app.cell.main import create_app

    image = os.environ.get("TSP_TEST_CELL_WORKER_IMAGE")
    pg_container = os.environ.get("TSP_TEST_POSTGRES_CONTAINER")
    if not image or not pg_container:
        pytest.skip("set TSP_TEST_CELL_WORKER_IMAGE and TSP_TEST_POSTGRES_CONTAINER")
    settings = cluster["settings"][0]
    tenant = "worker-smoke"
    provision_tenant(cluster["control"], cluster["data"]["a"], tenant, "Worker", "a", cluster["users"]["owner"])
    sync_catalog(cluster["control"])
    market = Market()
    day = datetime.now(UTC).date().isoformat()
    with TestClient(create_app(settings, market)) as client:
        token = client.post("/api/auth/login", json={"email": "owner@example.test", "password": PASSWORD}).json()["token"]
        headers = {"Authorization": "Bearer " + token, "X-Routing-Epoch": "1"}
        prefix = "/api/tenants/" + tenant
        client.post(prefix + "/watchlist", headers=headers, json={"symbols": ["000001.SZ"]})
        strategy = client.post(prefix + "/strategies", headers=headers, json={
            "name": "Smoke", "template_id": "ma_golden_cross", "overrides": {"basic_filter": {"enabled": False},
            "params": {"require_ma_golden": False, "use_volume_filter": False, "require_above_ma60": False}},
        }).json()["strategy"]
        client.post(prefix + f"/strategies/{strategy['id']}/run", headers=headers, json={"as_of": day, "idempotency_key": "smoke"})
        account = client.post(prefix + "/paper/accounts", headers=headers, json={"name": "Worker", "initial_cash": 10000}).json()["account"]
        assert client.post(prefix + f"/paper/accounts/{account['id']}/orders", headers=headers,
                           json={"symbol": "000001.SZ", "side": "buy", "qty": 100, "idempotency_key": "smoke"}).status_code == 201
        environment = os.environ.copy()
        environment["TSP_CELL_ID"] = "a"
        for variable, dsn in (("TSP_CELL_CONTROL_DSN", settings.control_dsn), ("TSP_CELL_DATA_DSN", settings.data_dsn)):
            environment[variable] = make_conninfo(dsn.get_secret_value(), host="127.0.0.1", port="5432")
        docker = ["docker"]
        if os.environ.get("TSP_TEST_DOCKER_CONTEXT"):
            docker += ["--context", os.environ["TSP_TEST_DOCKER_CONTEXT"]]
        script = """
import json, sys
from datetime import UTC, datetime
from app.cell.config import CellSettings
from app.cell.database import Databases
from app.cell.market import MarketQuote
from app.cell.worker import CellWorker
from app.cell.worker import run_compute
history = json.load(sys.stdin)
def compute(payload, timeout):
    try:
        return run_compute(payload, timeout)
    except Exception as exc:
        print(getattr(exc, 'stderr', type(exc).__name__), file=sys.stderr)
        raise
class Market:
    def history(self, *args):
        return history
    def quotes(self, symbols):
        return {s: MarketQuote(symbol=s,price=10,change_pct=.01,source_time=datetime.now(UTC),
            sequence=101,trading=True,suspended=False,limits_known=True,limit_up=11,limit_down=9) for s in symbols}
db = Databases(CellSettings())
db.open()
try:
    worker = CellWorker(db, Market(), compute)
    assert worker.process_jobs('worker-smoke') == 1
    assert worker.evaluate('worker-smoke')['fills'] == 1
finally:
    db.close()
"""
        result = subprocess.run(
            [*docker, "run", "--rm", "-i", "--read-only", "--tmpfs", "/tmp:size=256m",
             "--memory=2g", "--cpus=2", "--cap-drop=ALL", "--security-opt=no-new-privileges",
             "--network=container:" + pg_container, "-e", "TSP_CELL_ID",
             "-e", "TSP_CELL_CONTROL_DSN", "-e", "TSP_CELL_DATA_DSN", image, "python", "-c", script],
            input=json.dumps(market.history(day, ["000001.SZ"], 500)), env=environment,
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        job = client.get(prefix + "/strategy-jobs", headers=headers).json()["jobs"][0]
        assert job["status"] == "succeeded", (job, result.stderr)
        assert job["result"]["rows"][0]["symbol"] == "000001.SZ"
        assert len(client.get(prefix + f"/paper/accounts/{account['id']}", headers=headers).json()["fills"]) == 1
