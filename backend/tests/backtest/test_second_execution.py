from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.backtest.second_execution import (
    ExecutionConfig,
    OpeningLot,
    SecondOrder,
    validate_execution,
)
from app.backtest.second_replay import SecondReplayDataError, session_windows
from app.instrument_history import history_path, update_instrument_history
from app.market_time import CN_TZ
from app.services.second_quote_archive import build_snapshot_frame

DAY = date(2026, 1, 5)
SYMBOL = "600519.SH"


def _dataset(tmp_path, day=DAY, *, name="测试股票", known=True, book=10.01, start=None):
    start = start if start is not None else session_windows(day)[0][0]
    if known:
        update_instrument_history(
            tmp_path, pl.DataFrame({"symbol": [SYMBOL], "name": [name], "listing_date": [date(2000, 1, 1)]}),
            as_of=day - timedelta(days=1), source="test",
            available_at=datetime.combine(day - timedelta(days=1), datetime.min.time(), CN_TZ),
        )
    frames = []
    for i in range(4):
        stamp = start + i * 1000
        frames.append(build_snapshot_frame({"stock": [{
            "symbol": SYMBOL, "last_price": 10.0, "prev_close": 10.0,
            "volume": 1000.0 + i * 1000, "amount": 1_000_000.0 + i * 1_000_000,
            "source_timestamp": stamp, "provider_version": "1.12.2@test",
            "bid1": 10.0, "ask1": book, "bid1_volume": 1000, "ask1_volume": 1000,
        }]}, provider="rustdx", capture_id=i + 1, collected_at_ms=stamp, trade_date=day))
    path = tmp_path / "quote_second" / f"date={day}" / "hour=09" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.concat(frames).write_parquet(path)
    return start


def test_next_snapshot_latency_cash_and_t_plus_one(tmp_path):
    start = _dataset(tmp_path)
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=start + 3000,
        orders=[
            SecondOrder("buy", SYMBOL, start, "buy", 100),
            SecondOrder("sell", SYMBOL, start + 1000, "sell", 100),
        ],
    )
    assert len(result["fills"]) == 1
    fill = result["fills"][0]
    assert fill["fill_ms"] == start + 2000
    assert fill["price"] == 10.02
    assert fill["commission"] == 5
    assert result["ending_cash"] == 100_000 - 1002 - 5
    assert result["orders"][1]["reason"] == "t_plus_one_or_insufficient_position"
    assert result["data_manifest"]["fingerprint"] == result["quality"]["fingerprint"]


@pytest.mark.parametrize("day,rate", [(date(2023, 8, 25), .001), (date(2023, 8, 28), .0005)])
def test_sell_uses_historical_stamp_tax(day, rate, tmp_path):
    start = _dataset(tmp_path, day)
    result = validate_execution(
        tmp_path, day, start_ms=start, end_ms=start + 3000,
        orders=[SecondOrder("sell", SYMBOL, start, "sell", 1000)],
        opening_lots=[OpeningLot(SYMBOL, 1000, day - timedelta(days=1))],
        config=ExecutionConfig(slippage_bps=0),
    )
    assert result["fills"][0]["stamp_tax"] == 10_000 * rate


@pytest.mark.parametrize("known,name,reason", [
    (False, "测试", "historical_status_unknown"),
    (True, "*ST测试", "risk_warning_excluded"),
])
def test_missing_history_or_st_rejects(tmp_path, known, name, reason):
    start = _dataset(tmp_path, known=known, name=name)
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=start + 3000,
        orders=[SecondOrder("buy", SYMBOL, start, "buy", 100)],
    )
    assert not result["fills"]
    assert result["orders"][0]["reason"] == reason


def test_late_known_status_does_not_leak_into_signal(tmp_path):
    start = _dataset(tmp_path, known=False)
    update_instrument_history(
        tmp_path, pl.DataFrame({"symbol": [SYMBOL], "name": ["测试"], "listing_date": [date(2000, 1, 1)]}),
        as_of=DAY, source="test", available_at=datetime(2026, 1, 5, 16, tzinfo=CN_TZ),
    )
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=start + 3000,
        orders=[SecondOrder("buy", SYMBOL, start, "buy", 100)],
    )
    assert result["orders"][0]["reason"] == "historical_status_unknown"


def test_window_gap_rejects_entire_validation(tmp_path):
    start = _dataset(tmp_path)
    with pytest.raises(SecondReplayDataError, match="quality"):
        validate_execution(tmp_path, DAY, start_ms=start, end_ms=start + 30_000,
                           orders=[SecondOrder("buy", SYMBOL, start, "buy", 100)])


def test_cannot_fill_on_same_snapshot_or_at_limit(tmp_path):
    start = _dataset(tmp_path, book=11.0)
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=start + 3000,
        orders=[SecondOrder("buy", SYMBOL, start + 2000, "buy", 100)],
        config=ExecutionConfig(latency_ms=0),
    )
    assert not result["fills"]
    assert result["orders"][0]["reason"] == "price_limit"


def test_orders_share_sample_liquidity_budget(tmp_path):
    start = _dataset(tmp_path)
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=start + 3000,
        orders=[SecondOrder(str(i), SYMBOL, start + 2000, "buy", 1000) for i in range(2)],
        config=ExecutionConfig(latency_ms=0),
    )
    assert len(result["fills"]) == 1
    assert result["orders"][1]["reason"] == "insufficient_sampled_liquidity"


@pytest.mark.parametrize("changes,config,reason", [
    ({"ask1": None}, ExecutionConfig(), "empty_book"),
    ({"ask1_volume": 0.0}, ExecutionConfig(), "empty_book"),
    ({"volume": 1000.0}, ExecutionConfig(), "no_new_trading_volume"),
    ({}, ExecutionConfig(initial_cash=100), "insufficient_cash"),
    ({}, ExecutionConfig(latency_ms=2500), "no_post_arrival_interval"),
])
def test_unfillable_orders_do_not_create_cash_or_positions(tmp_path, changes, config, reason):
    start = _dataset(tmp_path)
    if changes:
        path = next((tmp_path / "quote_second").rglob("*.parquet"))
        frame = pl.read_parquet(path).with_columns([
            pl.lit(value, dtype=pl.Float64).alias(key) for key, value in changes.items()
        ])
        frame.write_parquet(path)
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=start + 3000,
        orders=[SecondOrder("buy", SYMBOL, start, "buy", 100)], config=config,
    )
    assert result["orders"][0]["reason"] == reason
    assert not result["fills"]
    assert not result["ending_lots"]
    assert result["ending_cash"] == config.initial_cash


def test_corrupt_history_is_a_data_error(tmp_path):
    start = _dataset(tmp_path)
    history_path(tmp_path).write_bytes(b"broken")
    with pytest.raises(SecondReplayDataError, match="invalid instrument history"):
        validate_execution(
            tmp_path, DAY, start_ms=start, end_ms=start + 3000,
            orders=[SecondOrder("buy", SYMBOL, start, "buy", 100)],
        )


def test_unknown_st_flag_cannot_be_normalized_into_non_st(tmp_path):
    start = _dataset(tmp_path)
    path = history_path(tmp_path)
    pl.read_parquet(path).with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_risk_warning")).write_parquet(path)
    with pytest.raises(SecondReplayDataError, match="unknown status flags"):
        validate_execution(
            tmp_path, DAY, start_ms=start, end_ms=start + 3000,
            orders=[SecondOrder("buy", SYMBOL, start, "buy", 100)],
        )


def test_interval_containing_pre_arrival_trades_cannot_fill(tmp_path):
    start = _dataset(tmp_path)
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=start + 3000,
        orders=[SecondOrder("buy", SYMBOL, start + 1500, "buy", 100)],
        config=ExecutionConfig(latency_ms=0),
    )
    assert result["fills"][0]["fill_ms"] == start + 3000


def test_closing_auction_snapshot_cannot_fill_continuous_order(tmp_path):
    end = session_windows(DAY)[1][1]
    start = _dataset(tmp_path, start=end - 3000)
    result = validate_execution(
        tmp_path, DAY, start_ms=start, end_ms=end,
        orders=[SecondOrder("buy", SYMBOL, end - 1000, "buy", 100)],
        config=ExecutionConfig(latency_ms=0),
    )
    assert result["quality"]["status"] == "ready"
    assert not result["fills"]
