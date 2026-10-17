"""Both sector consumers must isolate prices without censoring legitimate IPOs."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ext_data as api
from app.api import sector_rotation as rotation_api
from app.instrument_history import update_instrument_history
from app.services import rps_rotation, sector_rotation
from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.minute_adjust import mark_minute_basis_raw

DAY = "2026-10-16"
PREV = "2026-10-15"
SYMBOL = "600000.SH"


def write_frame(root, relative, frame):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)


def seed(root, *, closes=(110., 1800.), symbols=(SYMBOL, "600001.SH"),
         listing=date(2000, 1, 1), day=DAY, prev=PREV, name="普通股"):
    mark_minute_basis_raw(root)
    write_frame(root, f"kline_minute/date={day}/part.parquet", pl.DataFrame({
        "symbol": list(symbols),
        "datetime": [datetime.fromisoformat(f"{day}T09:35:00")] * len(symbols),
        "close": list(closes), "amount": [1000.] * len(symbols),
    }))
    write_frame(root, f"kline_daily/date={prev}/part.parquet", pl.DataFrame({
        "symbol": list(symbols), "close": [100.] * len(symbols),
    }))
    write_frame(root, "ext_data/ext_gn/part.parquet", pl.DataFrame({
        "symbol": list(symbols), "所属概念": ["测试板块"] * len(symbols),
    }))
    if listing:
        update_instrument_history(root, pl.DataFrame({
            "symbol": list(symbols), "name": [name] * len(symbols),
            "listing_date": [listing] * len(symbols),
        }), as_of=date.fromisoformat(day), source="test")


@pytest.fixture(autouse=True)
def reset():
    sector_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()
    yield
    sector_rotation.invalidate_cache()
    rps_rotation._map_cache.clear()
    rps_rotation._map_ts.clear()


def dimension(root):
    return api._dimension_intraday_compute(
        ExtConfig(id="ext_gn", label="概念", mode="snapshot", fields=[]),
        root, "所属概念", "测试板块", None,
    )


def rotation(root, monkeypatch):
    ext = pl.read_parquet(root / "ext_data/ext_gn/part.parquet")
    mapping = ext.select(
        pl.col("symbol").str.split(".").list.first().alias("_sym_up"),
        pl.lit("测试板块").alias("concept"),
    )
    monkeypatch.setattr(sector_rotation, "_load_concept_map_df", lambda *_: (mapping, 1))
    return sector_rotation.build_sector_rotation(SimpleNamespace(store=SimpleNamespace(data_dir=root)))


@pytest.mark.parametrize("consumer", ["dimension", "rotation"])
def test_outlier_excluded_with_coverage(tmp_path, monkeypatch, consumer):
    seed(tmp_path)
    result = dimension(tmp_path) if consumer == "dimension" else rotation(tmp_path, monkeypatch)
    assert result["status"] == "ok"
    if consumer == "dimension":
        assert result["points"][0]["sector"] == 0.1
        assert result["members_with_minute"] == 1
        assert result["coverage"] == 0.5
    else:
        assert result["sectors"][0]["pct_now"] == 0.1
        assert result["sectors"][0]["n_members_with_bars"] == 1
        assert result["sectors"][0]["coverage"] == 0.5
    assert result["quality"]["excluded_rows"] == 1


@pytest.mark.parametrize("consumer", ["dimension", "rotation"])
def test_all_anomalous_returns_empty_not_zero(tmp_path, monkeypatch, consumer):
    seed(tmp_path, closes=(1800.,), symbols=(SYMBOL,))
    result = dimension(tmp_path) if consumer == "dimension" else rotation(tmp_path, monkeypatch)
    assert result["status"] == "empty"
    assert result["reason"] == "minute_anomalies"
    assert result["quality"]["excluded_rows"] == 1


@pytest.mark.parametrize("symbol,listing,day,prev", [
    ("600000.SH", date(2026, 10, 12), DAY, PREV),
    ("300001.SZ", date(2026, 10, 12), DAY, PREV),
    ("688001.SH", date(2026, 10, 12), DAY, PREV),
    # Fifth trading day across National Day holiday, sixth day is bounded.
    ("600000.SH", date(2026, 9, 30), "2026-10-13", "2026-10-12"),
])
def test_ipo_unlimited_days_keep_large_legal_return(tmp_path, symbol, listing, day, prev):
    seed(tmp_path, closes=(180.,), symbols=(symbol,), listing=listing, day=day, prev=prev)
    result = dimension(tmp_path)
    assert result["points"][0]["sector"] == 0.8
    assert result["quality"]["unlimited_rows"] == 1
    assert result["quality"]["excluded_rows"] == 0


def test_sixth_ipo_trading_day_is_bounded(tmp_path):
    seed(tmp_path, closes=(180.,), symbols=(SYMBOL,), listing=date(2026, 9, 30),
         day="2026-10-14", prev="2026-10-13")
    result = dimension(tmp_path)
    assert result["status"] == "empty"
    assert result["quality"]["excluded_rows"] == 1


@pytest.mark.parametrize("symbol,close,name,day,prev", [
    (SYMBOL, 110., "普通股", DAY, PREV),
    (SYMBOL, 90., "普通股", DAY, PREV),
    ("300001.SZ", 120., "ST股票", DAY, PREV),
    ("688001.SH", 80., "普通股", DAY, PREV),
    (SYMBOL, 105., "ST股票", "2026-07-03", "2026-07-02"),
    (SYMBOL, 110., "ST股票", "2026-07-06", "2026-07-03"),
])
def test_legal_limits_preserved(tmp_path, symbol, close, name, day, prev):
    seed(tmp_path, closes=(close,), symbols=(symbol,), name=name, day=day, prev=prev)
    result = dimension(tmp_path)
    assert result["points"][0]["sector"] == pytest.approx(close / 100 - 1)
    assert result["quality"]["excluded_rows"] == 0


def test_ex_date_and_future_adjustment_use_same_raw_basis(tmp_path):
    seed(tmp_path, closes=(55.,), symbols=(SYMBOL,))
    write_frame(tmp_path, "adj_factor/factors.parquet", pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL],
        "trade_date": [date.fromisoformat(DAY), date(2026, 10, 19)],
        "ex_factor": [2., 2.],
    }))
    result = dimension(tmp_path)
    assert result["points"][0]["sector"] == 0.1  # raw 100 -> ex reference 50
    assert result["quality"]["excluded_rows"] == 0


def test_unknown_history_is_reported_without_ipo_guess(tmp_path):
    seed(tmp_path, closes=(180.,), symbols=(SYMBOL,), listing=None)
    result = dimension(tmp_path)
    assert result["points"][0]["sector"] == 0.8
    assert result["quality"]["unknown_rule_rows"] == 1


def test_explicit_rejected_minute_is_not_forward_filled(tmp_path):
    seed(tmp_path, closes=(101., 102.))
    rows = [
        {"symbol": symbol, "datetime": datetime.fromisoformat(f"{DAY}T{stamp}"), "close": close}
        for stamp, values in [("09:35:00", [101., 102.]), ("09:36:00", [1800., 104.])]
        for symbol, close in zip((SYMBOL, "600001.SH"), values, strict=True)
    ]
    write_frame(tmp_path, f"kline_minute/date={DAY}/part.parquet", pl.DataFrame(rows))
    result = dimension(tmp_path)
    assert result["points"][1]["sector"] == 0.04
    assert result["points"][1]["coverage"] == 0.5


def test_legacy_adjusted_partition_uses_first_price_and_reports_unknown(tmp_path):
    seed(tmp_path, closes=(25.,), symbols=(SYMBOL,))
    (tmp_path / "kline_minute/.raw_basis").unlink()
    write_frame(tmp_path, "adj_factor/factors.parquet", pl.DataFrame({
        "symbol": [SYMBOL], "trade_date": [date(2026, 10, 19)], "ex_factor": [4.],
    }))
    result = dimension(tmp_path)
    assert result["basis"] == "first_close"
    assert result["points"][0]["sector"] == 0.
    assert result["quality"]["unknown_rule_rows"] == 1


def test_wrong_reference_and_nonfinite_minutes_are_isolated(tmp_path):
    seed(tmp_path, closes=(100., float("nan"), float("inf"), 0., -1.),
         symbols=(SYMBOL, "600001.SH", "600002.SH", "600003.SH", "600004.SH"))
    # A reference stored in the wrong unit must not manufacture +9900%.
    write_frame(tmp_path, f"kline_daily/date={PREV}/part.parquet", pl.DataFrame({
        "symbol": [SYMBOL], "close": [1.],
    }))
    result = dimension(tmp_path)
    assert result["status"] == "empty"
    assert result["quality"]["excluded_rows"] == 5


def test_exchange_cent_rounding_preserves_low_price_limit(tmp_path):
    seed(tmp_path, closes=(1.16,), symbols=(SYMBOL,))
    write_frame(tmp_path, f"kline_daily/date={PREV}/part.parquet", pl.DataFrame({
        "symbol": [SYMBOL], "close": [1.05],
    }))
    result = dimension(tmp_path)
    assert result["points"][0]["sector"] == 0.1048
    assert result["quality"]["excluded_rows"] == 0


def test_st_rules_use_target_history_not_current_name(tmp_path):
    seed(tmp_path, closes=(108.,), symbols=(SYMBOL,), name="ST股票",
         day="2026-07-03", prev="2026-07-02")
    update_instrument_history(tmp_path, pl.DataFrame({
        "symbol": [SYMBOL], "name": ["普通股"], "listing_date": [date(2000, 1, 1)],
    }), as_of=date(2026, 10, 16), source="test")
    result = dimension(tmp_path)
    assert result["status"] == "empty"
    assert result["quality"]["excluded_rows"] == 1


def test_unreadable_reference_fails_explicitly(tmp_path):
    seed(tmp_path)
    (tmp_path / "instrument_status/history.parquet").write_bytes(b"broken")
    result = dimension(tmp_path)
    assert result["status"] == "no_data"
    assert result["reason"] == "minute_reference"


def test_rotation_keeps_rejected_last_bucket_as_gap(tmp_path, monkeypatch):
    seed(tmp_path, closes=(101.,), symbols=(SYMBOL,))
    write_frame(tmp_path, f"kline_minute/date={DAY}/part.parquet", pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL],
        "datetime": [datetime.fromisoformat(f"{DAY}T{stamp}") for stamp in ("09:35:00", "09:40:00")],
        "close": [101., 1800.], "amount": [1000., 1000.],
    }))
    result = rotation(tmp_path, monkeypatch)
    assert result["as_of"] == "09:40"
    assert result["timeline"][-1]["leader"] is None
    assert result["timeline"][-1]["rotation"] is None
    assert result["series"]["matrix"] == [[0.01, None]]
    assert result["universe"][0]["pct_now"] is None
    assert result["universe"][0]["coverage"] == 0
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(rotation_api.router)
    response = TestClient(app).get("/api/sector-rotation")
    assert response.status_code == 200
    assert response.json()["timeline"][-1]["rotation"] is None
    assert response.json()["quality"]["excluded_rows"] == 1


def test_rotation_all_members_rejected_with_other_valid_market_prices(tmp_path, monkeypatch):
    seed(tmp_path, closes=(1800., 110.))
    write_frame(tmp_path, "ext_data/ext_gn/part.parquet", pl.DataFrame({
        "symbol": [SYMBOL], "所属概念": ["测试板块"],
    }))
    result = rotation(tmp_path, monkeypatch)
    assert result["status"] == "empty"
    assert result["reason"] == "minute_anomalies"
    assert result["quality"]["valid_rows"] == 1


@pytest.mark.parametrize("consumer", ["dimension", "rotation"])
@pytest.mark.parametrize("malformed", [False, True])
def test_empty_or_malformed_partition_is_explicit(tmp_path, monkeypatch, consumer, malformed):
    seed(tmp_path)
    frame = pl.DataFrame({"symbol": [SYMBOL]}) if malformed else pl.DataFrame(schema={
        "symbol": pl.String, "datetime": pl.Datetime, "close": pl.Float64,
    })
    write_frame(tmp_path, f"kline_minute/date={DAY}/part.parquet", frame)
    result = dimension(tmp_path) if consumer == "dimension" else rotation(tmp_path, monkeypatch)
    assert result["status"] == "no_data"
    assert result["reason"] == ("minute_schema" if malformed else "minute_empty")


@pytest.mark.parametrize("case", ["partial", "all_rejected", "reference_error"])
def test_dimension_api_serializes_quality_and_failure_states(tmp_path, case):
    seed(tmp_path, closes=(1800., 1900.) if case == "all_rejected" else (110., 1800.))
    if case == "reference_error":
        (tmp_path / "instrument_status/history.parquet").write_bytes(b"broken")
    ExtConfigStore(tmp_path).upsert(ExtConfig(id="ext_gn", label="测试", mode="snapshot", fields=[]))
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(api.router)
    response = TestClient(app).get(
        "/api/ext-data/ext_gn/dimension-intraday",
        params={"field": "所属概念", "value": "测试板块"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["quality"]["total_rows"] == 2
    if case == "partial":
        assert data["points"][0]["sector"] == 0.1
        assert data["coverage"] == 0.5
    else:
        assert data["status"] == ("empty" if case == "all_rejected" else "no_data")
        assert data["points"] == []
