"""Unconfirmed books must not remove price-limit candidates from either API."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.api.screener import limit_ladder
from app.market_time import CN_TZ
from app.services import depth_service
from app.services.depth_service import DepthService
from app.services.market_overview_builder import build_market_overview
from app.services.screener import ScreenerService

DAY = date(2026, 10, 20)
SYMBOL = "301030.SZ"


def _book(hour=14, minute=30, **overrides):
    book = {
        "ask_prices": [4.69],
        "bid_prices": [4.68],
        "ask_volumes": [5110],
        "bid_volumes": [1705],
        "received_at": datetime(2026, 10, 20, hour, minute, tzinfo=CN_TZ).timestamp() * 1000,
    }
    book.update(overrides)
    return book


def _service(tmp_path):
    service = DepthService()
    service.set_repo(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    return service


@pytest.mark.parametrize(
    "book",
    [
        _book(15, 2),
        _book(14, 58),
        _book(12, 55),
        _book(9, 25),
        _book(ask_prices=[]),
        _book(bid_volumes=[]),
        _book(ask_volumes=[-1]),
        _book(ask_volumes=[float("nan")]),
        _book(received_at=0),
        _book(received_at=datetime(2026, 10, 19, 14, 30, tzinfo=CN_TZ).timestamp() * 1000),
        _book(ask_prices=[4.7], bid_prices=[4.7]),
    ],
)
def test_unconfirmed_book_is_pending(tmp_path, book):
    service = _service(tmp_path)
    service._seal_locked({SYMBOL: book}, [SYMBOL], [], DAY, False, {SYMBOL: 4.7})
    entry = service.get_sealed_map(DAY, False)[SYMBOL]
    assert entry["sealed"] is None
    assert entry["ready"] is False


@pytest.mark.parametrize("is_down", [False, True])
def test_valid_intraday_unsealed_book_survives_disk_restore(tmp_path, monkeypatch, is_down):
    monkeypatch.setattr(depth_service, "cn_now", lambda: datetime(2026, 10, 20, 14, 30, tzinfo=CN_TZ))
    service = _service(tmp_path)
    book = _book() if not is_down else _book(ask_prices=[3.94], bid_prices=[3.93])
    reference = 4.7 if not is_down else 3.92
    service._seal_locked(
        {SYMBOL: book}, [] if is_down else [SYMBOL], [SYMBOL] if is_down else [],
        DAY, True, {SYMBOL: reference},
    )
    for reader in (service, _service(tmp_path)):
        entry = reader.get_sealed_map(DAY, is_down)[SYMBOL]
        assert entry["sealed"] is False
        assert entry["ready"] is True


def test_intraday_unsealed_verdict_expires_at_close(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._seal_locked({SYMBOL: _book()}, [SYMBOL], [], DAY, True, {SYMBOL: 4.7})
    monkeypatch.setattr(depth_service, "cn_now", lambda: datetime(2026, 10, 20, 15, 2, tzinfo=CN_TZ))
    assert service.get_sealed_map(DAY, False)[SYMBOL]["sealed"] is None
    assert _service(tmp_path).get_sealed_map(DAY, False)[SYMBOL]["sealed"] is None


def test_fetch_compares_depth_with_raw_price(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._repo.get_enriched_latest = lambda: (pl.DataFrame({
        "symbol": [SYMBOL], "signal_limit_up": [True],
        "close": [2.35], "raw_close": [4.7],
    }), DAY)
    monkeypatch.setattr(depth_service, "cn_now", lambda: datetime(2026, 10, 20, 15, 2, tzinfo=CN_TZ))
    monkeypatch.setattr(service, "_call_depth_batch", lambda *args: {
        SYMBOL: _book(15, 2, ask_prices=[0], ask_volumes=[0], bid_prices=[4.7]),
    })
    service._fetch_and_seal(True)
    assert service.get_sealed_map(DAY, False)[SYMBOL]["sealed"] is True


@pytest.mark.parametrize("is_down", [False, True])
def test_sealed_close_requires_orders_at_reference_price(tmp_path, is_down):
    service = _service(tmp_path)
    book = _book(15, 2, ask_prices=[0], ask_volumes=[0], bid_prices=[4.7])
    if is_down:
        book = _book(15, 2, bid_prices=[0], bid_volumes=[0], ask_prices=[4.7])
    service._seal_locked(
        {SYMBOL: book}, [] if is_down else [SYMBOL], [SYMBOL] if is_down else [],
        DAY, True, {SYMBOL: 4.7},
    )
    assert _service(tmp_path).get_sealed_map(DAY, is_down)[SYMBOL]["sealed"] is True


def test_missing_candidate_is_pending_and_extra_symbol_ignored(tmp_path):
    service = _service(tmp_path)
    service._seal_locked(
        {"600000.SH": _book()}, [SYMBOL], [], DAY, False, {SYMBOL: 4.7},
    )
    entries = service.get_sealed_map(DAY, False)
    assert set(entries) == {SYMBOL}
    assert entries[SYMBOL]["sealed"] is None
    assert entries[SYMBOL]["ready"] is False


def test_legacy_false_snapshot_does_not_override_price_limit(tmp_path, monkeypatch):
    part = tmp_path / "depth5/date=2026-10-20/part.parquet"
    part.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": [SYMBOL], "sealed_up": [False], "sealed_down": [None],
        "ask1_vol": [5110], "bid1_vol": [1705], "status": ["limit_up"],
        "fetched_at": [1792479721.602],
    }).write_parquet(part)
    service = _service(tmp_path)
    snapshot = pl.DataFrame({
        "symbol": [SYMBOL], "name": ["*ST仕净"], "close": [4.7],
        "_sealed_vol": [5110.0],
        "signal_limit_up": [True], "signal_limit_down": [False],
        "signal_broken_limit_up": [False], "consecutive_limit_ups": [1],
    })
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", lambda *args: snapshot)
    monkeypatch.setattr(ScreenerService, "load_prior_consecutive", lambda *args: pl.DataFrame())
    overview = build_market_overview(service._repo, None, service, DAY)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=service._repo, depth_service=service,
    )))
    ladder = limit_ladder(request, DAY, "up", None)
    assert overview["limit"]["limit_up"] == 1
    assert ladder["counts"]["up"] == 1
    assert ladder["tiers"][0]["stocks"][0]["status"] == "limit_up"
    assert ladder["tiers"][0]["stocks"][0]["sealed_vol"] == 1705
    service._restore_from_parquet(DAY)
    assert service.get_sealed_map(DAY, False)[SYMBOL]["sealed"] is None


def test_only_ready_current_candidates_can_be_deducted(tmp_path, monkeypatch):
    service = _service(tmp_path)
    snapshot = pl.DataFrame({
        "symbol": [SYMBOL], "close": [4.7],
        "signal_limit_up": [True], "signal_limit_down": [False],
        "signal_broken_limit_up": [False], "consecutive_limit_ups": [1],
    })
    monkeypatch.setattr(ScreenerService, "_load_enriched_for_date", lambda *args: snapshot)
    monkeypatch.setattr(ScreenerService, "load_prior_consecutive", lambda *args: pl.DataFrame())
    monkeypatch.setattr(service, "get_sealed_map", lambda *args, **kwargs: {
        SYMBOL: {"sealed": False, "ready": False, "vol": 1705},
        "600000.SH": {"sealed": False, "ready": True, "vol": 123},
    })
    monkeypatch.setattr(service, "is_sealed_ready", lambda *args: True)
    overview = build_market_overview(service._repo, None, service, DAY)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=service._repo, depth_service=service,
    )))
    ladder = limit_ladder(request, DAY, "up", None)
    assert overview["limit"]["limit_up"] == 1
    assert overview["limit"]["fake_up"] == 0
    assert ladder["counts"] == {"up": 1, "down": 0}
    assert ladder["sealed_counts_up"] == {"real": 0, "fake": 0, "pending": 1}
    assert ladder["tiers"][0]["stocks"][0]["status"] == "limit_up"
