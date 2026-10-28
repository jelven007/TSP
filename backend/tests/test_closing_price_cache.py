"""收盘未确认时必须更新真实展示价, 且不得写盘或覆盖权威收盘。"""
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

import app.services.quote_service as qs_module
from app.cell.platform_market import full_snapshot
from app.enriched_generation import get_enriched_generation
from app.indicators.pipeline import compute_enriched
from app.market_time import CN_TZ, cn_today
from app.multitenancy.market import decode_frame
from app.multitenancy.quote_bridge import PublicQuoteBridge
from app.services.quote_service import QuoteService
from app.tickflow.repository import DataStore, KlineRepository


def _ms(hour, minute, second=0):
    return int(datetime.combine(cn_today(), time(hour, minute, second), CN_TZ).timestamp() * 1000)


def _record(symbol="002084.SZ", price=7.10, **extra):
    return {
        "symbol": symbol, "last_price": price, "open": 7.0, "high": 7.2, "low": 6.9,
        "volume": 100_000.0, "amount": price * 100_000,
        "timestamp": _ms(15, 1), "timestamp_basis": "received", **extra,
    }


@pytest.fixture
def live(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    repo.clear_cache()
    symbols = ["002084.SZ", "600000.SH", "000001.SH"]
    history = pl.DataFrame([
        {
            "symbol": symbol, "date": cn_today() - timedelta(days=offset),
            "open": 7.0, "high": 7.2, "low": 6.9, "close": 7.0,
            "volume": 90_000.0, "amount": 630_000.0,
        }
        for symbol in symbols for offset in range(100, 0, -1)
    ])
    qs = QuoteService()
    qs._repo = repo
    monkeypatch.setattr(repo, "get_live_agg", pl.DataFrame)
    monkeypatch.setattr(repo, "get_instruments", pl.DataFrame)
    monkeypatch.setattr(qs, "_load_enriched_history_window", lambda *a: history)
    monkeypatch.setattr(qs_module, "_persist_last_fetch", lambda ms: None)
    events = []
    monkeypatch.setattr(qs, "_broadcast_quote_updated", lambda: events.append("updated"))

    for asset, selected in (("stock", symbols[:2]), ("index", symbols[2:])):
        records = [
            _record(symbol, 7.07, timestamp=_ms(14, 59, 59), timestamp_basis="source_snapshot")
            for symbol in selected
        ]
        daily = qs._build_daily(records)
        enriched = compute_enriched(pl.concat([history, daily], how="diagonal_relaxed"))
        repo.flush_live_daily_asset(asset, daily)
        repo.flush_live_enriched_asset(asset, enriched.filter(pl.col("date") == cn_today()).filter(
            pl.col("symbol").is_in(selected)
        ))
    repo.rebuild_views()
    repo._enriched_history_cache = compute_enriched(history)
    yield qs, repo, events
    repo.db.close()


def _files(repo):
    return {path: path.read_bytes() for path in repo.store.data_dir.rglob("*.parquet")}


def _price(repo, symbol="002084.SZ", asset="stock"):
    frame, day = repo.get_enriched_latest_asset(asset, refresh=False)
    assert day == cn_today()
    return frame.filter(pl.col("symbol") == symbol).row(0, named=True)


def test_received_close_updates_actual_prices_without_persisting(live):
    qs, repo, events = live
    before, generation = _files(repo), get_enriched_generation(repo.store.data_dir)
    history = repo._enriched_history_cache
    result = qs._process_full_market_records(
        [_record(), _record("000001.SH")],
        t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    for asset, symbol in (("stock", "002084.SZ"), ("index", "000001.SH")):
        row = _price(repo, symbol, asset)
        assert row["raw_close"] == pytest.approx(7.10)
        assert row["close"] == pytest.approx(7.10)
        assert row["quote_ts"] == _ms(15, 1)
        assert row["quote_time_basis"] == "received"
    assert _price(repo, "600000.SH")["raw_close"] == 7.07
    assert _files(repo) == before
    assert get_enriched_generation(repo.store.data_dir) == generation
    assert repo._enriched_history_cache is history
    assert qs._last_final_confirmed is False
    assert result is None  # 未交给监控评估
    assert events == ["updated"]


@pytest.mark.parametrize("kind", ["batch", "confirmed"])
def test_authoritative_close_cannot_be_reverted(live, kind):
    qs, repo, _ = live
    frame, _ = repo.get_enriched_latest()
    frame = frame.with_columns(
        pl.lit(7.10).alias("raw_close"), pl.lit(7.10).alias("close"),
        pl.lit(None if kind == "batch" else _ms(15, 0), dtype=pl.Int64).alias("quote_ts"),
        pl.lit(None if kind == "batch" else "source_snapshot", dtype=pl.String).alias("quote_time_basis"),
    )
    repo.flush_live_enriched_asset("stock", frame)
    before = _files(repo)
    qs._process_full_market_records(
        [_record(price=7.07)], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    assert _price(repo)["raw_close"] == 7.10
    assert _files(repo) == before


def test_partial_confirmed_snapshot_updates_only_present_valid_rows(live, monkeypatch):
    qs, repo, _ = live
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["002084.SZ", "600000.SH"]}))
    before = _files(repo)
    qs._process_full_market_records(
        [_record(timestamp_basis="source_snapshot")], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    assert _price(repo)["raw_close"] == pytest.approx(7.10)
    assert _price(repo, "600000.SH")["raw_close"] == 7.07
    assert qs._last_final_confirmed is False
    assert _files(repo) == before


@pytest.mark.parametrize("bad", [
    {"timestamp": _ms(15, 1) - 86_400_000},
    {"open": 0, "high": 0, "volume": 0, "amount": 0},
    {"last_price": float("nan")},
    {"last_price": float("inf")},
    {"last_price": 0.0},
])
def test_invalid_or_halted_row_does_not_freeze_other_symbols(live, bad):
    qs, repo, _ = live
    before = _files(repo)
    qs._process_full_market_records(
        [_record(**bad), _record("600000.SH")], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    assert _price(repo)["raw_close"] == 7.07
    assert _price(repo, "600000.SH")["raw_close"] == pytest.approx(7.10)
    assert _files(repo) == before


def test_conflicting_duplicates_keep_previous_price_and_update_other_symbols(live):
    qs, repo, _ = live
    qs._process_full_market_records(
        [_record(), _record(price=7.20), _record("600000.SH")],
        t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    assert _price(repo)["raw_close"] == 7.07
    assert _price(repo, "600000.SH")["raw_close"] == pytest.approx(7.10)


def test_delayed_provisional_snapshot_does_not_move_price_backwards(live):
    qs, repo, _ = live
    qs._process_full_market_records(
        [_record()], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    qs._process_full_market_records(
        [_record(price=7.06, timestamp=_ms(14, 59, 58))],
        t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    assert _price(repo)["raw_close"] == pytest.approx(7.10)


@pytest.mark.parametrize("asset", ["stock", "index"])
def test_restart_preserves_quote_clock_and_batch_authority(live, asset):
    _, repo, _ = live
    frame, _ = repo.get_enriched_latest_asset(asset)
    restarted = KlineRepository(DataStore(repo.store.data_dir))
    loaded, _ = restarted.get_enriched_latest_asset(asset)
    assert loaded["quote_ts"].to_list() == frame["quote_ts"].to_list()
    assert loaded["quote_time_basis"].to_list() == frame["quote_time_basis"].to_list()
    restarted.db.close()
    batch = frame.with_columns(
        pl.lit(7.10).alias("raw_close"), pl.lit(7.10).alias("close"),
        pl.lit(None, dtype=pl.Int64).alias("quote_ts"),
        pl.lit(None, dtype=pl.String).alias("quote_time_basis"),
    )
    repo.flush_live_enriched_asset(asset, batch)
    restarted = KlineRepository(DataStore(repo.store.data_dir))
    restarted.get_enriched_latest_asset(asset)
    restarted.update_provisional_enriched_asset(asset, frame)
    row = _price(restarted, frame["symbol"][0], asset)
    assert row["close"] == 7.10
    if asset != "index":
        assert row["raw_close"] == 7.10
    restarted.db.close()


def test_pipeline_cache_swap_during_calculation_wins(live, monkeypatch):
    qs, repo, _ = live
    original = qs._load_enriched_history_window

    def settle(*args):
        frame, _ = repo.get_enriched_latest()
        batch = frame.with_columns(
            pl.lit(7.10).alias("raw_close"), pl.lit(7.10).alias("close"),
            pl.lit(None, dtype=pl.Int64).alias("quote_ts"),
            pl.lit(None, dtype=pl.String).alias("quote_time_basis"),
        )
        repo.flush_live_enriched_asset("stock", batch)
        return original(*args)

    monkeypatch.setattr(qs, "_load_enriched_history_window", settle)
    qs._process_full_market_records(
        [_record(price=7.07)], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    assert _price(repo)["raw_close"] == 7.10


def test_public_snapshot_and_bridge_read_updated_price_after_notification(live, monkeypatch):
    qs, repo, _ = live
    state = SimpleNamespace(repo=repo, quote_service=qs)
    qs.set_app_state(state)
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "test-market-token")
    monkeypatch.setattr(qs, "status", lambda: {"is_trading_hours": False, "last_fetch_ms": 1})
    monkeypatch.setattr(qs, "_broadcast_quote_updated", lambda: QuoteService._broadcast_quote_updated(qs))
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    initial = full_snapshot(request, "Bearer test-market-token")
    assert decode_frame(initial["frames"]["stock"])["raw_close"].to_list() == [7.07, 7.07]
    subscriber = qs.subscribe()
    qs._process_full_market_records(
        [_record()], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    assert subscriber.pop()["quote_updated"] is True
    updated = full_snapshot(request, "Bearer test-market-token")
    assert updated is not initial
    public_repo = KlineRepository(DataStore(repo.store.data_dir, read_only=True))
    bridge = PublicQuoteBridge(public_repo, market=None)
    bridge.update(updated)
    assert _price(public_repo)["raw_close"] == pytest.approx(7.10)
    assert _price(public_repo)["quote_time_basis"] == "received"
    public_repo.db.close()


def test_disk_refresh_broadcasts_new_authoritative_cache(live):
    qs, repo, _ = live
    state = SimpleNamespace(full_public_snapshot=("old", {}))
    qs.set_app_state(state)
    repo._on_refresh_done = lambda: QuoteService._broadcast_quote_updated(qs)
    frame, _ = repo.get_enriched_latest()
    batch = frame.with_columns(
        pl.lit(7.10).alias("raw_close"), pl.lit(7.10).alias("close"),
        pl.lit(None, dtype=pl.Int64).alias("quote_ts"),
        pl.lit(None, dtype=pl.String).alias("quote_time_basis"),
    )
    repo.flush_live_enriched_asset("stock", batch)
    subscriber = qs.subscribe()
    repo.refresh_cache()
    assert _price(repo)["raw_close"] == 7.10
    assert subscriber.pop()["quote_updated"] is True
    assert state.full_public_snapshot is None


def test_incremental_provisional_cache_preserves_raw_and_adjusted_price(live, monkeypatch):
    qs, repo, _ = live
    repo._enriched_history_cache = repo._enriched_history_cache.with_columns(
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_ups"),
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_downs"),
    )
    repo._build_live_agg(cn_today() - timedelta(days=1))
    assert repo._live_agg_cache is not None and not repo._live_agg_cache.is_empty()
    agg = repo._live_agg_cache.with_columns(pl.lit(0.5).alias("_adj_factor"))
    monkeypatch.setattr(repo, "get_live_agg", lambda: agg)
    monkeypatch.setattr(qs, "_load_enriched_history_window", lambda *a: pytest.fail("incremental path fell back"))
    before = _files(repo)
    qs._process_full_market_records(
        [_record()], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0),
    )
    row = _price(repo)
    assert row["raw_close"] == pytest.approx(7.10)
    assert row["close"] == pytest.approx(3.55)
    assert row["quote_ts"] == _ms(15, 1)
    assert row["quote_time_basis"] == "received"
    assert _files(repo) == before


def test_empty_snapshot_keeps_all_cached_prices(live):
    qs, repo, _ = live
    before = _files(repo)
    qs._process_full_market_records([], t0=0, now_ts=0, final_boundary_ms=_ms(15, 0))
    assert _price(repo)["raw_close"] == 7.07
    assert _price(repo, "600000.SH")["raw_close"] == 7.07
    assert _files(repo) == before
