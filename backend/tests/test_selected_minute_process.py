"""真实spawn采集; 不连接行情服务, 子进程内安装本地Provider夹具。"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from test_minute_refresh import _svc

DAY = "2026-11-02"


def _fixture_worker(connection):
    """monkeypatch不能穿过spawn, 夹具须在子进程内替换解析边界。"""
    from app.data_providers import custom
    from app.services import kline_sync
    from app.services.selected_minute_process import _worker

    class Provider:
        def get_intraday_batch_with_count(self, symbols):
            mode = os.environ.get("TSP_SELECTED_TEST_MODE", "")
            root = Path(os.environ["TSP_SELECTED_TEST_ROOT"])
            (root / "fetch-started").write_text(str(os.getpid()))
            if mode == "exit":
                os._exit(7)
            if mode == "block":
                while not (root / "release").exists():
                    time.sleep(0.01)
            if mode == "fail" and not (root / "recovered").exists():
                raise kline_sync.MinuteFetchError("fixture failure", requests=2)
            # 同时返回无关成员和重复时标, 实际边界和落盘须裁剪/去重。
            return pl.DataFrame({
                "symbol": [*symbols, symbols[0], "600099.SH"],
                "datetime": [datetime(2026, 11, 2, 14, 30)] * (len(symbols) + 2),
                "close": [10.] * (len(symbols) + 2),
            }), 1

        get_intraday_batch = get_intraday_batch_with_count

    kline_sync._resolve_minute_provider = lambda name: (Provider(), False, None)
    custom.load_all = lambda: None
    if os.environ.get("TSP_SELECTED_TEST_MODE") == "live":
        from app import market_time

        market_time.cn_now = lambda: datetime(2026, 11, 2, 14, 30, 5)
    _worker(connection)

def _loader_worker(connection):
    """真实注册表加载临时YAML; 仅取数函数用本地帧隔离网络。"""
    from app.config import settings
    from app.data_providers.custom import loader
    from app.services import kline_sync
    from app.services.selected_minute_process import _worker

    settings.platform_config_dir = None
    loader._load_builtin_plugins = lambda: None
    def fetch(provider, name, symbols):
        assert provider.config.name == name == "fixture"
        return pl.DataFrame({"symbol": symbols,
                             "datetime": [datetime(2026, 11, 2, 14, 30)] * len(symbols),
                             "close": [12.] * len(symbols)}), 1
    kline_sync.fetch_intraday_custom_batch = fetch
    _worker(connection)


def _job(tmp_path, *, symbols=("600001.SH",)):
    return {"data_dir": str(tmp_path / "source"), "day": DAY,
            "provider": "fixture", "symbols": list(symbols), "mode": "full",
            "capabilities": {}}


def _supervisor(tmp_path, monkeypatch, *, mode="", timeout=10):
    from app.services.selected_minute_process import SelectedMinuteProcess
    monkeypatch.setenv("TSP_SELECTED_TEST_MODE", mode)
    monkeypatch.setenv("TSP_SELECTED_TEST_ROOT", str(tmp_path))
    return SelectedMinuteProcess(worker_target=_fixture_worker, timeout_seconds=timeout)


def _until(probe, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("selected process did not reach the expected state")


def test_spawn_writes_only_selected_normalized_rows_and_reports_real_pid(tmp_path, monkeypatch):
    worker = _supervisor(tmp_path, monkeypatch)
    job = _job(tmp_path, symbols=("600001.SH", "600002.SH"))
    try:
        assert worker.advance(job) is None
        pid = worker.status()["pid"]
        reply = _until(lambda: worker.advance(job))
        assert pid != os.getpid() and reply["pid"] == pid
        assert reply["requested_symbols"] == reply["covered_symbols"] == 2
        assert reply["written_rows"] == 2 and reply["requests"] == 1
        frame = pl.read_parquet(tmp_path / f"source/kline_minute/date={DAY}/part.parquet")
        assert frame["symbol"].to_list() == job["symbols"]
        assert not worker.status()["inflight"]
        assert "symbols" not in worker.status()
    finally:
        worker.close()
    assert worker.status()["pid"] is None


def test_spawn_delivers_verified_slice_while_actual_partition_lock_is_held(tmp_path, monkeypatch):
    from app.services.kline_sync import _minute_partition_lock

    worker = _supervisor(tmp_path, monkeypatch, mode="live")
    job = {**_job(tmp_path), "live_handoff": True}
    partition = tmp_path / f"source/kline_minute/date={DAY}/part.parquet"
    partition.parent.mkdir(parents=True)
    observed = []

    def receive():
        return worker.advance(job, on_verified=lambda frame, seq: observed.append((frame, seq)))

    try:
        with _minute_partition_lock(partition):
            def verify():
                assert receive() is None
                return bool(observed)

            _until(verify)
            frame, seq = observed[0]
            assert frame["symbol"].unique().to_list() == job["symbols"] and seq == 1
            assert not partition.exists() and worker.status()["inflight"]
        reply = _until(receive)
        assert reply["error"] is None and reply["written_rows"] == 1 and partition.exists()
    finally:
        worker.close()


def test_spawn_loads_controlled_source_configuration_itself(tmp_path):
    from app.services.selected_minute_process import SelectedMinuteProcess
    root = tmp_path / "source/data_sources"
    root.mkdir(parents=True)
    fields = ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount")
    (root / "fixture.yaml").write_text(json.dumps({
        "name": "fixture", "datasets": {"minute": {
            "url": "https://example.test/minute", "field_map": {n: n for n in fields},
        }},
    }))
    worker = SelectedMinuteProcess(worker_target=_loader_worker)
    job = _job(tmp_path)
    try:
        worker.advance(job)
        reply = _until(lambda: worker.advance(job))
        assert reply["error"] is None
        assert reply["written_rows"] == 1
    finally:
        worker.close()


@pytest.mark.parametrize("field,value", [
    ("day", "2026-11-03"), ("provider", "fixture2"),
    ("symbols", ["600002.SH"]), ("source_generation", 2),
])
def test_member_day_or_provider_change_replaces_idle_child(tmp_path, monkeypatch, field, value):
    worker = _supervisor(tmp_path, monkeypatch)
    job = _job(tmp_path)
    try:
        worker.advance(job)
        reply = _until(lambda: worker.advance(job))
        pid = reply["pid"]
        worker.advance({**job, field: value})
        assert worker.status()["pid"] != pid
    finally:
        worker.close()


def test_older_selected_response_cannot_overwrite_newer_full_market_write(tmp_path, monkeypatch):
    from app.services.kline_sync import _write_minute_partition
    from app.services.minute_write_order import begin_minute_request
    worker = _supervisor(tmp_path, monkeypatch, mode="block")
    job = _job(tmp_path)
    try:
        worker.advance(job)
        _until(lambda: (tmp_path / "fetch-started").exists())
        root = Path(job["data_dir"]) / "kline_minute"
        sequence = begin_minute_request(root)
        _write_minute_partition(pl.DataFrame({
            "symbol": job["symbols"], "datetime": [datetime(2026, 11, 2, 14, 30)],
            "close": [20.],
        }), root, request_seq=sequence)
        (tmp_path / "release").touch()
        reply = _until(lambda: worker.advance(job))
        assert reply["request_seq"] < sequence and reply["written_rows"] == 0
        assert pl.read_parquet(root / f"date={DAY}/part.parquet")["close"].to_list() == [20.]
    finally:
        worker.close()


@pytest.mark.parametrize("mode,error", [("exit", "exited"), ("fail", "fixture failure")])
def test_failure_retains_reason_and_restarts_child(tmp_path, monkeypatch, mode, error):
    worker = _supervisor(tmp_path, monkeypatch, mode=mode)
    job = _job(tmp_path)
    try:
        worker.advance(job)
        first_pid = worker.status()["pid"]
        reply = _until(lambda: worker.advance(job))
        assert error in reply["error"]
        if mode == "fail":
            assert reply["requests"] == 2
        monkeypatch.setenv("TSP_SELECTED_TEST_MODE", "")
        (tmp_path / "recovered").touch()
        worker.advance(job)
        reply = _until(lambda: worker.advance(job))
        assert reply["error"] is None
        assert (reply["pid"] != first_pid) if mode == "exit" else (reply["pid"] == first_pid)
    finally:
        worker.close()


def test_timeout_cancellation_and_unchanged_heartbeat_do_not_queue_batches(tmp_path, monkeypatch):
    worker = _supervisor(tmp_path, monkeypatch, mode="block", timeout=1.5)
    job = _job(tmp_path)
    try:
        worker.advance(job)
        _until(lambda: (tmp_path / "fetch-started").exists())
        pid = worker.status()["pid"]
        assert worker.advance(dict(job)) is None
        assert worker.status()["pid"] == pid
        reply = _until(lambda: worker.advance(job))
        assert "timed out" in reply["error"] and worker.status()["pid"] is None
        worker.advance(job)
        assert worker.status()["inflight"]
        worker.advance(None)
        assert worker.status()["pid"] is None and not worker.status()["inflight"]
    finally:
        worker.close()

def test_missing_selected_data_is_failure_with_coverage_not_successful_zero(tmp_path, monkeypatch):
    worker = _supervisor(tmp_path, monkeypatch)
    # Provider返回上一日, 规范化后不可作为当日成功输入。
    job = {**_job(tmp_path), "day": "2026-11-03"}
    try:
        worker.advance(job)
        reply = _until(lambda: worker.advance(job))
        assert "no current-day" in reply["error"]
        assert reply["requests"] == 1 and reply["covered_symbols"] == 0
        assert reply["missing_symbols"] == 1 and reply["written_rows"] == 0
        assert not (tmp_path / "source/kline_minute/date=2026-11-03/part.parquet").exists()
    finally:
        worker.close()


def test_increment_fallback_reuses_single_child_and_new_request_sequence(tmp_path, monkeypatch):
    worker = _supervisor(tmp_path, monkeypatch)
    job = _job(tmp_path)
    try:
        worker.advance(job)
        first = _until(lambda: worker.advance(job))
        worker.advance({**job, "mode": "increment"})
        second = _until(lambda: worker.advance({**job, "mode": "increment"}))
        assert first["pid"] == second["pid"]
        assert second["request_seq"] > first["request_seq"]
        assert second["error"] is None and second["written_rows"] == 1
    finally:
        worker.close()


def test_selected_publication_completes_while_full_market_fetch_is_blocked(tmp_path, monkeypatch):
    from app.enriched_generation import get_enriched_generation
    from app.multitenancy.publication import PublicPublisher, release_path
    worker = _supervisor(tmp_path, monkeypatch)
    custom = SimpleNamespace(get_intraday_latest=lambda **kwargs: None)
    svc = _svc(tmp_path, monkeypatch, minute_provider="fixture", custom=custom)
    svc._selected_process = worker
    svc._repo.store.data_dir = tmp_path / "source"
    svc._repo._inst = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"]})
    for asset in ("stock", "index"):
        get_enriched_generation(svc._repo.store.data_dir, asset)
    publisher = PublicPublisher(svc._repo.store.data_dir, tmp_path / "public")
    svc._app_state.public_publisher = publisher
    monkeypatch.setattr("app.services.minute_refresh.cn_now",
                        lambda: datetime(2026, 11, 2, 14, 30, 15))
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    entered, release = threading.Event(), threading.Event()

    def full_fetch(provider, name, *, symbols, count=3):
        assert symbols == ["600001.SH", "600002.SH"]
        entered.set()
        assert release.wait(10)
        return pl.DataFrame(), 1

    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_custom_latest", full_fetch)
    svc.set_priority("tenant", {"day": DAY, "sequence": 1,
                              "symbols": ["600001.SH", "600002.SH"],
                              "selected": ["600001.SH"]})
    try:
        svc.start()
        assert entered.wait(5)
        public = _until(lambda: release_path(publisher.root)
                        if (publisher.root / "current").exists() else None)
        frame = pl.read_parquet(public / f"kline_minute/date={DAY}/part.parquet")
        assert frame["symbol"].to_list() == ["600001.SH"]
        status = svc.status()["priority"]
        assert status["pid"] != os.getpid() and status["last_error"] is None
        manifest = json.loads((public / ".public_release.json").read_text())
        assert manifest["minute_priority_completed_at"] is not None
        assert "minute_completed_at" not in manifest
        assert svc._state.rounds == 0
    finally:
        svc.stop()
        release.set()
        svc._thread.join(5)
    assert worker.status()["pid"] is None


def test_service_disabled_gate_cancels_selected_child(tmp_path, monkeypatch):
    from app.services import preferences
    worker = _supervisor(tmp_path, monkeypatch, mode="block")
    svc = _svc(tmp_path, monkeypatch)
    svc._selected_process = worker
    monkeypatch.setattr("app.services.minute_refresh.cn_now",
                        lambda: datetime(2026, 11, 2, 14, 30))
    svc.set_priority("tenant", {"day": DAY, "sequence": 1,
                              "symbols": ["600001.SH"], "selected": ["600001.SH"]})
    try:
        svc._selected_tick()
        assert worker.status()["inflight"]
        preferences.save({"data_sync_auto_enabled": False})
        svc._selected_tick()
        assert worker.status()["pid"] is None
        assert svc.status()["priority"]["gate_reason"] == "disabled"
    finally:
        svc.stop()
