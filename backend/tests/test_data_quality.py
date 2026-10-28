from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import data_quality


def test_latest_quality_explains_partial_coverage_and_invalid_values(tmp_path):
    partition = tmp_path / "date=2026-10-23"
    partition.mkdir()
    pl.DataFrame({
        "symbol": ["600001.SH", "000001.SZ"],
        "open": [10., 11.], "high": [12., 9.], "low": [9., 8.], "close": [11., 10.],
        "volume": [100., -1.],
    }).write_parquet(partition / "part.parquet")
    result = data_quality.latest_partition_quality(tmp_path, 4)
    assert result["coverage_ratio"] == .5
    assert result["invalid_price_rows"] == result["invalid_volume_rows"] == 1
    assert result["basis"] == "latest_partition_current_catalog"


def test_missing_or_corrupt_data_stays_unknown(tmp_path):
    result = data_quality.latest_partition_quality(tmp_path, None)
    assert result["coverage_ratio"] is result["rows"] is None
    partition = tmp_path / "date=2026-10-23"
    partition.mkdir()
    (partition / "part.parquet").write_text("broken")
    result = data_quality.latest_partition_quality(tmp_path, 4)
    assert result["coverage_ratio"] is None
    assert result["error"]


def test_quality_inspection_respects_limit(tmp_path, monkeypatch):
    partition = tmp_path / "date=2026-10-23"
    partition.mkdir()
    (partition / "part.parquet").write_bytes(b"123")
    monkeypatch.setattr(data_quality, "MAX_PARTITION_BYTES", 2)
    result = data_quality.latest_partition_quality(tmp_path, 4)
    assert result["error"] == "partition_inspection_limit"
    assert result["invalid_price_rows"] is None


def test_initialization_distinguishes_failure_success_and_independent_features(tmp_path, monkeypatch):
    monkeypatch.setattr(data_quality, "audit_provider_routes", lambda: {
        "routes": {"minute": {"usable": False}},
    })
    jobs = [
        {"id": "failed", "kind": "daily_pipeline", "status": "failed", "finished_at": "new"},
        {"id": "success", "kind": "daily_pipeline", "status": "succeeded", "finished_at": "old"},
    ]
    result = data_quality.initialization_status(
        tmp_path, {"daily": {"rows": 4}}, jobs, auto_enabled=True, indicators_ready=False,
    )
    assert result["status"] == "retry_required"
    assert result["last_successful_pipeline_at"] == "old"
    assert result["stages"]["minute"] == "unavailable"
    assert result["stages"]["enriched"] == "warming"
    assert result["stages"]["ai"] == result["stages"]["notifications"] == "unknown"
    (tmp_path / "initial_sync.json").write_text('{"status":"completed"}')
    result = data_quality.initialization_status(
        tmp_path, {}, jobs, auto_enabled=False, indicators_ready=True,
    )
    assert result["status"] == "completed"


def test_status_http_exposes_unknown_historical_evidence(tmp_path, monkeypatch):
    from app.api import data
    from app.services import pipeline_jobs, preferences

    monkeypatch.setattr(data, "_get_table_stats", lambda name, fetch, **kwargs: fetch() if name == "quality" else None)
    monkeypatch.setattr(data, "_get_storage", lambda path: {})
    monkeypatch.setattr(data, "_last_finished", lambda label: None)
    monkeypatch.setattr(pipeline_jobs, "job_store", SimpleNamespace(list_recent=lambda limit: []))
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(data_quality, "audit_provider_routes", lambda: {"routes": {}})
    app = FastAPI()
    app.include_router(data.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    result = TestClient(app).get("/api/data/status").json()
    assert result["initialization"]["status"] == "pending"
    assert result["quality"]["historical_pit_verified"] is False
    assert result["quality"]["historical_inputs"]["universe"]["missing_delisted_ratio"] is None
    assert result["quality"]["latest_daily"]["coverage_ratio"] is None


def test_business_availability_uses_real_cache_and_isolates_public_releases(tmp_path, monkeypatch):
    from app.api import data
    from app.multitenancy import bootstrap
    from app.services.public_query_cache import PublicQueryCache

    monkeypatch.setattr(data, "public_query_cache", PublicQueryCache())
    monkeypatch.setattr(data_quality, "audit_provider_routes", lambda: {"routes": {}})
    monkeypatch.setattr(data, "_safe_aggregate_enriched", lambda repo: {"rows": repo.rows})
    monkeypatch.setattr(data, "_safe_aggregate_daily", lambda repo: {"rows": repo.rows})
    monkeypatch.setattr(data, "_safe_aggregate_instruments", lambda repo: {
        "symbols_covered": repo.symbols,
    })
    monkeypatch.setattr(data, "_safe_aggregate_financials", lambda repo: None)
    roots = [tmp_path / "release-one", tmp_path / "release-two"]
    for root in roots:
        partition = root / "kline_daily" / "date=2026-10-23"
        partition.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["000001.SZ"], "open": [10.], "high": [12.],
            "low": [9.], "close": [11.], "volume": [100.],
        }).write_parquet(partition / "part.parquet")
    app = FastAPI()
    app.include_router(bootstrap.router)
    app.state.account_identity = SimpleNamespace(current_user=lambda cookie: {
        "permissions": ["business:read"],
    })
    app.state.indicators_ready = True
    with TestClient(app) as client:
        for root, rows, symbols in zip(roots, (11, 22), (4, 8), strict=True):
            app.state.repo = SimpleNamespace(
                store=SimpleNamespace(data_dir=root), rows=rows, symbols=symbols,
            )
            response = client.get("/api/business/data-availability")
            assert response.status_code == 200
            result = response.json()
            assert set(result) == {"enriched", "indicators_ready", "quality"}
            assert result["enriched"] == {"rows": rows}
            assert result["quality"]["datasets"]["daily"]["rows"] == rows
            assert result["quality"]["latest_daily"]["coverage_ratio"] == 1 / symbols
            assert result["quality"]["historical_pit_verified"] is False
