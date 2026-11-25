"""The first validated phase is immutable; local acknowledgement never gates online."""
import importlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from test_strategy_input_manifest import build, minutes, request


@pytest.fixture(autouse=True)
def source_algorithm(monkeypatch):
    monkeypatch.setattr("app.services.strategy_input_binding.algorithm_sha256", lambda: "b" * 64)


def store(root):
    name = "app.services.strategy_input_authority"
    assert importlib.util.find_spec(name), "single S02 input authority is missing"
    return importlib.import_module(name).InputStore(root)


def test_pending_phase_is_not_sealed(tmp_path):
    service = store(tmp_path)
    pending = build(minutes().head(1))
    with pytest.raises(ValueError, match="就绪"):
        service.seal(pending)
    assert service.read(request(), "stocks") is None


def test_atomic_first_writer_seal_survives_restart_and_different_late_prices(tmp_path):
    service = store(tmp_path)
    first = build(minutes())
    late = deepcopy(first)
    late["input"]["minutes"][0]["close"] += .01
    from app.services.strategy_input_manifest import SEMANTIC_FIELDS, digest
    late["input_sha256"] = digest({key: late[key] for key in SEMANTIC_FIELDS})
    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(service.seal, (first, late)))
    assert values[0]["input_sha256"] == values[1]["input_sha256"]
    assert store(tmp_path).read(request(), "stocks")["input_sha256"] == values[0]["input_sha256"]


def test_common_key_does_not_use_independent_pool_or_selection_values(tmp_path):
    service = store(tmp_path)
    sealed = service.seal(build(minutes()))
    local = request()
    local["selection"]["rows"][0]["atr_rank"] = 99
    local["pool"]["symbols"] = local["pool"]["symbols"][:1]
    received = service.read(local, "stocks")
    assert received["input_sha256"] == sealed["input_sha256"]
    assert received["input"]["selection"]["rows"][0]["atr_rank"] == 1
    changed = request()
    changed["definition_sha256"] = "f" * 64
    assert service.read(changed, "stocks") is None


def test_consumer_rejects_foreign_definition_or_algorithm(tmp_path):
    from app.services.strategy_input_manifest import bind_manifest
    value = build(minutes())
    for field in ("definition_sha256", "algorithm_sha256"):
        changed = request()
        changed[field] = "f" * 64
        with pytest.raises(ValueError, match="身份"):
            bind_manifest(value, changed, "stocks")


def test_phase_store_does_not_need_local_confirmation(tmp_path):
    service = store(tmp_path)
    stocks = service.seal(build(minutes()))
    assert service.read(request(), "breadth") is None
    assert service.read(request(), "stocks") == stocks


def test_other_phase_cannot_seal_a_different_selection_under_the_same_run(tmp_path):
    service = store(tmp_path)
    service.seal(build(minutes()))
    req = request()
    req["selection"]["rows"][0]["atr_rank"] = 99
    with pytest.raises(ValueError, match="基线"):
        service.seal(build(minutes(), phase="breadth", value=req))


def test_background_wait_repairs_only_the_missing_member(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    import polars as pl
    from test_strategy_input_manifest import AT, SYMBOLS

    from app.market_time import CN_TZ
    from app.services.strategy_input_authority import S02InputService

    monkeypatch.setattr("app.market_time.cn_now", lambda: (AT + timedelta(seconds=40)).replace(tzinfo=CN_TZ))
    calls = []
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data")),
        monitor_input_repair=SimpleNamespace(request=lambda value: calls.append(value) or {
            "id": "repair-id", "state": "queued"}))
    service = S02InputService(state)
    req = request()
    req["retry_until"] = "2026-11-24T14:55:00+08:00"
    frame = minutes().filter(~((pl.col("symbol") == SYMBOLS[1]) & (pl.col("datetime") == AT)))
    monkeypatch.setattr(service, "_inputs", lambda *_: dict(
        minutes=frame, references={s: 10. for s in SYMBOLS}, reference_errors={},
        coverage={}, source_evidence={}))
    try:
        service._advance(req, "stocks")
        assert calls[0]["symbols"] == [SYMBOLS[1]]
        assert calls[0]["daily_dates"] == []
        assert calls[0]["minute_end"] == "14:30:00"
        from app.services.strategy_input_manifest import common_key
        assert service.status[(common_key(req), "stocks")]["repairs"][0]["id"] == "repair-id"
        assert service.store.read(req, "stocks") is None
    finally:
        service.close()


def test_source_repair_fetches_only_missing_history_dates(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from test_strategy_input_manifest import AT, SYMBOLS

    from app.market_time import CN_TZ
    from app.services.strategy_input_authority import S02InputService
    monkeypatch.setattr("app.market_time.cn_now", lambda: (AT + timedelta(seconds=40)).replace(tzinfo=CN_TZ))
    calls = []
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data")),
        monitor_input_repair=SimpleNamespace(request=lambda value: calls.append(value) or {"id": "repair"}))
    service = S02InputService(state)
    req = request()
    req["retry_until"] = "2026-11-24T14:55:00+08:00"
    try:
        service._repair(req, "stocks", {"coverage": {SYMBOLS[0]: {
            "state": "pending", "missing_minutes": [], "missing_reference": False,
            "missing_daily_dates": ["2026-11-03", "2026-11-04"]}}})
        assert calls[0]["daily_dates"] == ["2026-11-03", "2026-11-04"]
        assert calls[0]["symbols"] == [SYMBOLS[0]]
        assert calls[0]["minute_end"] is None
    finally:
        service.close()


def test_transient_publication_error_remains_waiting_and_preserves_repair_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services.strategy_input_authority import S02InputService
    from app.services.strategy_input_manifest import common_key
    service = S02InputService(SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data"))))
    req = request()
    job = (common_key(req), "stocks")
    service.status[job] = {"state": "waiting_input", "repairs": [{"id": "prior"}]}
    monkeypatch.setattr(service, "_inputs", lambda *_: (_ for _ in ()).throw(OSError("publisher busy")))
    try:
        service._advance(req, "stocks")
        assert service.status[job]["state"] == "waiting_input"
        assert service.status[job]["repairs"] == [{"id": "prior"}]
    finally:
        service.close()


def test_no_archive_waits_before_any_market_io(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from test_strategy_input_manifest import AT

    from app.market_time import CN_TZ
    from app.services.strategy_input_authority import S02InputService
    monkeypatch.setattr("app.market_time.cn_now", lambda: (AT + timedelta(seconds=40)).replace(tzinfo=CN_TZ))
    service = S02InputService(SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data"))))
    req = request()
    req["retry_until"] = "2026-11-24T14:55:00+08:00"
    req["selection"] = {}
    monkeypatch.setattr(service, "_inputs", lambda *_: pytest.fail("missing archive used market IO"))
    try:
        result = service.request(req, "stocks")
        assert result["state"] == "waiting_input"
        assert result["reason"] == "等待精确日期预选归档"
        assert not service.active
    finally:
        service.close()


def test_public_request_omits_private_fields_and_rejects_unbounded_window():
    from app.services import strategy_input_manifest as manifest
    assert hasattr(manifest, "normalize_request"), "public S02 admission is unbounded"
    req = request()
    req["retry_until"] = "2026-11-24T14:55:00+08:00"
    req["selection"]["account"] = {"balance": 1234}
    req["selection"]["rows"][0]["account_id"] = "private"
    normalized = manifest.normalize_request(req)
    assert "account" not in normalized["selection"]
    assert "account_id" not in normalized["selection"]["rows"][0]
    with pytest.raises(ValueError, match="期限"):
        manifest.normalize_request({**req, "retry_until": "2026-11-24T23:00:00+08:00"})


def test_stock_seal_does_not_freeze_a_missing_nonselected_breadth_reference(tmp_path):
    from app.services.strategy_input_manifest import SEMANTIC_FIELDS, digest
    req = request()
    req["selection"]["rows"] = req["selection"]["rows"][:1]
    stocks = build(minutes(), value=req)
    missing_symbol = req["pool"]["symbols"][1]
    stocks["input"]["references"][missing_symbol] = None
    stocks["input"]["source_evidence"]["reference_daily"] = [
        row for row in stocks["input"]["source_evidence"]["reference_daily"]
        if row["symbol"] != missing_symbol]
    stocks["input_sha256"] = digest({key: stocks[key] for key in SEMANTIC_FIELDS})
    service = store(tmp_path)
    service.seal(stocks)
    breadth = build(minutes(), phase="breadth", value=req)
    assert service.seal(breadth)["state"] == "ready"


def test_expired_authority_request_performs_no_new_collection(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from test_strategy_input_manifest import AT

    from app.market_time import CN_TZ
    from app.services.strategy_input_authority import S02InputService
    monkeypatch.setattr("app.market_time.cn_now", lambda: (AT + timedelta(minutes=26)).replace(tzinfo=CN_TZ))
    service = S02InputService(SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data"))))
    req = request()
    req["retry_until"] = "2026-11-24T14:55:00+08:00"
    monkeypatch.setattr(service, "_inputs", lambda *_: pytest.fail("expired request performed collection"))
    try:
        assert service.request(req, "stocks")["state"] == "expired_input"
        assert not service.active
    finally:
        service.close()


def test_publisher_refuses_foreign_algorithm_before_collection(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from test_strategy_input_manifest import AT

    from app.market_time import CN_TZ
    from app.services.strategy_input_authority import S02InputService
    monkeypatch.setattr("app.market_time.cn_now", lambda: (AT + timedelta(seconds=40)).replace(tzinfo=CN_TZ))
    service = S02InputService(SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data"))))
    req = request()
    req.update(algorithm_sha256="f" * 64, retry_until="2026-11-24T14:55:00+08:00")
    try:
        result = service.request(req, "stocks")
        assert result["state"] == "invalid_input"
        assert "计算实现" in result["reason"]
        assert not service.active
    finally:
        service.close()


@pytest.mark.parametrize("field,bad", [
    ("pool", []), ("selection", []), ("signal", []),
    ("pool", {"symbols": ["600001.SH", "600002.SH", "600001.SH"]}),
    ("selection", {"rows": [None]}),
    ("signal", {"id": "late_session_strength", "params": None}),
])
def test_malformed_nested_requests_have_no_background_side_effects(field, bad):
    from app.services.strategy_input_manifest import normalize_request
    req = {**request(), "retry_until": "2026-11-24T14:55:00+08:00", field: bad}
    with pytest.raises(ValueError):
        normalize_request(req)
