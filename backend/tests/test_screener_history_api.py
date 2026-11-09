from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import screener
from app.services import strategy_results
from app.strategy import config
from app.strategy.engine import StrategyDataContext, StrategyEngine, StrategyResult


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / "strategies"
    path.mkdir()
    (path / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "样例", "asset_types": ["stock", "etf"]}
def filter(df, params):
    return pl.lit(True)
''')
    engine = StrategyEngine([path])
    calls = []

    class Service:
        def __init__(self, repo, asset_type="stock"):
            self.asset_type = asset_type

        def latest_date(self):
            return date(2026, 10, 23)

        def build_strategy_context(self, engine, as_of, *args, timeframe="1d", **kwargs):
            return StrategyDataContext(self.asset_type, timeframe, as_of)

    def run(sid, context, **kwargs):
        calls.append(context.as_of)
        return StrategyResult(context.as_of, sid, [{"symbol": str(context.as_of)}], 1)

    monkeypatch.setattr(engine, "run", run)
    monkeypatch.setattr(screener, "ScreenerService", Service)
    monkeypatch.setattr(screener, "_load_ext_value_maps", lambda *_: {})
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                            strategy_engine=engine)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return tmp_path, engine, request, calls


def test_first_single_run_persists_and_date_switch_reads_without_calculation(runtime):
    _, _, request, calls = runtime
    for day in ("2026-10-22", "2026-10-23"):
        screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of=day), request)
    payload = screener.get_cached_result("sample", request, as_of=date(2026, 10, 22))
    assert payload["result"]["rows"] == [{"symbol": "2026-10-22"}]
    assert len(calls) == 2
    summary = screener.get_cached_summary(request, as_of=date(2026, 10, 22))
    assert summary["results"]["sample"]["total"] == 1
    assert "rows" not in summary["results"]["sample"]


def test_signal_sections_survive_single_batch_and_cached_result(runtime, monkeypatch):
    _, engine, request, _ = runtime
    day = date(2026, 11, 2)
    sections = {"sell": [], "select": [{"symbol": "SELECT"}], "buy": [{"symbol": "BUY"}]}
    def result():
        return StrategyResult(day, "sample", sections["buy"], 1, sections=sections,
                              section_mode="signals")
    monkeypatch.setattr(engine, "run", lambda *a, **kw: result())
    monkeypatch.setattr(engine, "run_all", lambda *a, **kw: {"sample": result()})
    monkeypatch.setattr(screener, "_coverage_warnings", lambda *a, **kw: [])
    single = screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of=day), request)
    batch = screener.run_all(request, {"as_of": str(day), "strategy_ids": ["sample"]})["results"]["sample"]
    cached = screener.get_cached_result("sample", request, as_of=day)["result"]
    for item in (single, batch, cached):
        assert item["sections"]["select"] == [{"symbol": "SELECT"}]
        assert item["sections"]["buy"] == [{"symbol": "BUY"}]
        assert item["section_mode"] == "signals"
        assert "2026-10-30" in item["section_errors"]["sell"]


def test_future_preselection_is_readable_with_metadata_without_running_strategy(runtime):
    from app.services.daily_selections import DailySelections
    storage, engine, request, calls = runtime
    engine.get("sample").meta["timeframes"] = ["1m"]
    target = date(2026, 11, 6)
    identity = strategy_results.capture_identity(engine, storage, "sample", timeframe="1m")
    selected = {"as_of": str(target), "source_date": "2026-11-05", "rows": [],
                "scheduled_for": "2026-11-05T16:00:00+08:00",
                "generated_at": "2026-11-05T16:01:00+08:00"}
    DailySelections(storage).save(identity, selected, started_at=1)
    payload = screener.get_cached_result("sample", request, as_of=target, timeframe="1m")["result"]
    assert payload["selection_only"] is True
    assert payload["selection_snapshot"]["source_date"] == "2026-11-05"
    assert payload["sections"]["select"] == []
    assert payload["section_errors"]["buy"]
    assert calls == []


def test_repeated_single_and_batch_runs_share_unchanged_daily_archive(runtime, monkeypatch):
    import app.services.screener as service
    from app.api import strategy

    storage, engine, request, _ = runtime
    monkeypatch.setattr(service, "ScreenerService", screener.ScreenerService)
    monkeypatch.setattr(screener, "_coverage_warnings", lambda *a, **kw: [])
    monkeypatch.setattr(engine, "run_all", lambda context, **kw: {
        "sample": StrategyResult(context.as_of, "sample", [{"symbol": str(context.as_of)}], 1),
    })
    day = "2026-10-22"
    body = screener.PresetRequest(strategy_id="sample", as_of=day)
    screener.run_preset(body, request)
    key = strategy_results.capture_identity(engine, storage, "sample")
    store = strategy_results.StrategyResults(storage)
    first = store.read(key, day)
    screener.run_preset(body, request)
    screener.run_all(request, {"as_of": day})
    strategy.run_strategy(strategy.RunRequest(strategy_id="sample", as_of=day), request)
    strategy.run_all(strategy.RunAllRequest(as_of=day), request)
    repeated = store.read(key, day)
    assert repeated["result"] == first["result"]
    assert repeated["_cache_token"] == first["_cache_token"]
    assert repeated["summary"]["computed_at"] == first["summary"]["computed_at"]
    assert len(screener.get_result_history("sample", request, limit=30, cursor=None)["records"]) == 1


def test_changed_parameters_miss_but_old_snapshot_is_readable(runtime):
    storage, _, request, _ = runtime
    screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of="2026-10-22"), request)
    config.save_override(storage, "sample", {"params": {"threshold": 11}})
    assert screener.get_cached_result("sample", request, as_of=date(2026, 10, 22))["result"] is None
    page = screener.get_result_history("sample", request, limit=30, cursor=None)
    assert len(page["records"]) == 1
    record = page["records"][0]
    assert record["params"] == {}
    detail = screener.get_result_history_detail("sample", record["record_id"], request)
    assert detail["result"]["rows"] == [{"symbol": "2026-10-22"}]


def test_historical_read_never_overlays_latest_monitor(runtime):
    _, _, request, _ = runtime
    screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of="2026-10-22"), request)
    request.app.state.monitor_engine = SimpleNamespace(
        latest_strategy_results=lambda: {"sample": {"as_of": "2026-10-23", "rows": [{"symbol": "LIVE"}]}},
    )
    assert screener.get_cached_result("sample", request, as_of=date(2026, 10, 22))[
        "result"
    ]["rows"] == [{"symbol": "2026-10-22"}]


def test_persistence_failure_is_explicit_and_previous_success_survives(runtime, monkeypatch):
    storage, engine, request, _ = runtime
    screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of="2026-10-22"), request)
    key = strategy_results.capture_identity(engine, storage, "sample")
    monkeypatch.setattr(strategy_results.StrategyResults, "save",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk failed")))
    with pytest.raises(HTTPException) as error:
        screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of="2026-10-22"), request)
    assert error.value.status_code == 503
    assert strategy_results.StrategyResults(storage).read(key, "2026-10-22")["result"]["total"] == 1


def test_legacy_strategy_entrypoints_also_archive_full_rows(runtime, monkeypatch):
    import app.services.screener as service
    from app.api import strategy

    storage, engine, request, _ = runtime
    monkeypatch.setattr(service, "ScreenerService", screener.ScreenerService)
    strategy.run_strategy(strategy.RunRequest(strategy_id="sample", as_of="2026-10-22"), request)
    key = strategy_results.capture_identity(engine, storage, "sample")
    assert strategy_results.StrategyResults(storage).read(key, "2026-10-22")[
        "result"
    ]["rows"] == [{"symbol": "2026-10-22"}]
    monkeypatch.setattr(engine, "run_all", lambda context, **kw: {
        "sample": StrategyResult(context.as_of, "sample", [{"symbol": "BATCH"}], 1),
    })
    strategy.run_all(strategy.RunAllRequest(as_of="2026-10-23"), request)
    assert strategy_results.StrategyResults(storage).read(key, "2026-10-23")[
        "result"
    ]["rows"] == [{"symbol": "BATCH"}]


def test_minute_missing_requested_day_does_not_fall_back_to_latest(runtime):
    import polars as pl

    from app.services.screener import ScreenerService

    repo = SimpleNamespace(
        get_minute_by_dates=lambda *_: pl.DataFrame(),
        latest_minute_date_global=lambda: date(2026, 10, 23),
    )
    with pytest.raises(ValueError, match="2026-10-22"):
        ScreenerService(repo)._load_minute_history(date(2026, 10, 22), pl.DataFrame({"symbol": ["A"]}))


def test_live_result_requires_current_date_and_matching_loaded_configuration(runtime, monkeypatch):
    from app.market_time import cn_today
    from app.strategy.version import runtime_identity

    _, engine, request, _ = runtime
    day = cn_today()
    screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of=day), request)
    live = {"as_of": str(day), "total": 1, "rows": [{"symbol": "LIVE"}],
            "asset_type": "stock", "timeframe": "1d",
            "runtime_identity": runtime_identity(engine.get("sample"), {})}
    request.app.state.monitor_engine = SimpleNamespace(latest_strategy_results=lambda: {"sample": live})
    assert screener.get_cached_result("sample", request, as_of=day)["result"]["rows"] == [{"symbol": "LIVE"}]
    live["runtime_identity"] = "old-configuration"
    assert screener.get_cached_result("sample", request, as_of=day)["result"]["rows"] == [{"symbol": str(day)}]


def test_saved_detail_keeps_other_strategy_membership_for_selected_symbols(runtime):
    from dataclasses import replace

    storage, engine, request, _ = runtime
    engine._strategies["other"] = replace(engine.get("sample"), meta={"id": "other", "name": "另一策略"})
    for sid in ("sample", "other"):
        key = strategy_results.capture_identity(engine, storage, sid)
        strategy_results.StrategyResults(storage).save(
            key, {"as_of": "2026-10-22", "total": 1, "rows": [{"symbol": "A"}]}, started_at=1,
        )
    payload = screener.get_cached_result("sample", request, as_of=date(2026, 10, 22))
    assert payload["strategy_ids_by_symbol"] == {"A": ["other", "sample"]}


def test_history_http_filters_validation_and_delete(runtime):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _, _, request, _ = runtime
    for day in ("2026-10-22", "2026-10-23"):
        screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of=day), request)
    app = FastAPI()
    app.state.repo = request.app.state.repo
    app.include_router(screener.router)
    with TestClient(app) as client:
        url = "/api/screener/result-history/sample"
        response = client.get(url, params={"q": "样例", "start_date": "2026-10-22",
                                          "end_date": "2026-10-22", "asset_type": "stock",
                                          "timeframe": "1d"})
        assert response.status_code == 200
        assert [r["as_of"] for r in response.json()["records"]] == ["2026-10-22"]
        for filters in ({"start_date": "bad"}, {"asset_type": "index"}, {"timeframe": "bad"},
                        {"start_date": "2026-10-23", "end_date": "2026-10-22"}):
            assert client.get(url, params=filters).status_code == 400
        record_id = response.json()["records"][0]["record_id"]
        assert client.delete(f"/api/screener/result-history/other/{record_id}").status_code == 404
        assert client.delete(f"{url}/{record_id}").status_code == 200
        assert client.get(f"{url}/{record_id}").status_code == 404
        assert client.delete(f"{url}/{record_id}").status_code == 404
        assert [r["as_of"] for r in client.get(url).json()["records"]] == ["2026-10-23"]


def test_history_delete_failure_is_explicit_and_preserves_previous_result(runtime, monkeypatch):
    storage, engine, request, _ = runtime
    screener.run_preset(screener.PresetRequest(strategy_id="sample", as_of="2026-10-22"), request)
    key = strategy_results.capture_identity(engine, storage, "sample")
    monkeypatch.setattr(strategy_results.StrategyResults, "delete",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("private disk path")))
    with pytest.raises(HTTPException) as error:
        screener.delete_result_history("sample", key.record_id("2026-10-22"), request)
    assert error.value.status_code == 503
    assert "private disk path" not in error.value.detail
    assert strategy_results.StrategyResults(storage).read(key, "2026-10-22") is not None


def test_day_view_http_returns_saved_sections_without_engine_or_market_reads(runtime, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    storage, engine, request, _ = runtime
    key = strategy_results.capture_identity(engine, storage, "sample", timeframe="1m")
    sections = {"sell": [], "select": [{"symbol": "SELECT", "score": 88}], "buy": []}
    strategy_results.StrategyResults(storage).save(
        key, {"as_of": "2026-10-22", "rows": [], "total": 0,
              "section_mode": "signals", "sections": sections}, started_at=1,
    )
    def forbidden(*args, **kwargs):
        raise AssertionError("historical GET attempted strategy/market access")
    monkeypatch.setattr(screener, "_strategy_engine", forbidden)
    monkeypatch.setattr(screener, "_load_ext_value_maps", forbidden)
    app = FastAPI()
    app.state.repo = request.app.state.repo
    app.include_router(screener.router)
    with TestClient(app) as client:
        url = "/api/screener/day-view/sample"
        response = client.get(url, params={"as_of": "2026-10-22"})
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert response.json()["result"]["sections"] == sections
        assert response.json()["summary"]["strategy_version"] == key.strategy_version
        assert client.get(url, params={"as_of": "2026-10-23"}).json()["result"] is None
        plain = client.get(url, params={"as_of": "2026-10-22"}, headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers
        assert plain.json() == response.json()
        for encoding in ("gzip;q=invalid", "gzip;q=0", "gzip;q=0.0"):
            denied = client.get(url, params={"as_of": "2026-10-22"},
                                headers={"Accept-Encoding": encoding})
            assert denied.status_code == 200
            assert "content-encoding" not in denied.headers
            assert denied.json() == response.json()
        assert client.get(url, params={"as_of": "bad"}).status_code == 422
        assert client.get(url, params={"as_of": "2026-10-22", "timeframe": "1d"}).status_code == 422


def test_day_view_keeps_authentication_but_skips_market_and_factor_middleware(runtime, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.cell.context import AccessError
    from app.multitenancy import access
    from app.multitenancy.documents import TenantDocuments
    storage, _, _, _ = runtime
    calls = []
    account = {"id": "owner", "default_tenant_id": "tenant", "auth_version": 1,
               "permissions": ["business:read"]}
    def current_user(cookie):
        calls.append("authenticate")
        if not cookie:
            raise AccessError(401, "unauthorized")
        return account
    def context(*args):
        calls.append("tenant")
        return object()
    def forbidden(*args):
        raise AssertionError("historical GET loaded market/factor dependencies")
    service = SimpleNamespace(db=object(), current_user=current_user, context=context)
    app = FastAPI()
    app.state.account_identity = service
    app.state.quote_service = SimpleNamespace(ensure_fresh=forbidden)
    app.middleware("http")(access.middleware)
    app.include_router(screener.router)
    monkeypatch.setattr(TenantDocuments, "values", forbidden)
    monkeypatch.setattr(screener, "_private_data_dir", lambda request: storage)
    with TestClient(app) as client:
        from app.api.auth import COOKIE_NAME
        url = "/api/screener/day-view/sample?as_of=2026-06-22"
        assert client.get(url).status_code == 401
        client.cookies.set(COOKIE_NAME, "fixture-session")
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["x-tsp-account-scope"] == "tsp-v1:owner:tenant:1"
        assert response.json()["availability"] == "not_materialized"
    assert calls == ["authenticate", "authenticate", "tenant"]
