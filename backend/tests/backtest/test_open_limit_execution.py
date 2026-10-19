"""Daily OHLC cannot establish queue fills at the opening price limit."""
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import (
    build_market_data_matrix,
    build_market_matrix_from_signals,
    load_market_data_matrix_from_parquet,
    make_signal_matrix,
)

METHODS = [
    "simulate_portfolio", "simulate_independent_candidates",
    "simulate_portfolio_legacy", "simulate_independent_candidates_legacy",
]


def panel(*, opening=11.0, closing=10.5, scale=1.0, symbol="600001.SH"):
    rows = []
    for i in range(5):
        o, c = (opening, closing) if i == 2 else (10.0, 10.0)
        rows.append({
            "symbol": symbol, "name": "正常公司",
            "date": date(2026, 7, 1) + timedelta(days=i),
            "open": o * scale, "close": c * scale,
            "high": max(o, c) * scale, "low": min(o, c) * scale,
            "raw_close": c, "volume": 100_000.0,
            "signal_limit_up": False, "signal_limit_down": False,
        })
    return pl.DataFrame(rows)


def run(method, df, *, buy_day=1, sell_day=2, **kwargs):
    cfg = MatcherConfig(
        matching="open_t+1", fees_pct=0, slippage_bps=0, max_positions=1, **kwargs,
    )
    return getattr(BacktestEngine(None), method)(
        df, pl.Series([i == buy_day for i in range(len(df))]),
        pl.Series([i == sell_day for i in range(len(df))]), cfg,
    )


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("scale", [1.0, 0.4])
def test_open_limit_up_then_unlock_does_not_fill_or_retry_stale_buy(method, scale):
    result = run(method, panel(scale=scale))
    assert not result.trades
    assert result.stats["execution"]["buy_limit_up"] == 1


@pytest.mark.parametrize("method", METHODS)
def test_open_limit_down_then_unlock_preserves_pending_sale(method):
    result = run(method, panel(opening=9, closing=9.5), buy_day=0, sell_day=1)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_date == "2026-07-04"
    assert trade.exit_price == 10.0
    assert trade.blocked_exit_days >= 1


@pytest.mark.parametrize("method", METHODS)
def test_gap_stop_at_limit_down_cannot_bypass_queue_check(method):
    result = run(
        method, panel(opening=9, closing=9.5), buy_day=0, sell_day=-1,
        stop_loss_pct=0.06,
    )
    assert result.trades[0].exit_date == "2026-07-04"
    assert result.trades[0].exit_reason == "stop_loss"


@pytest.mark.parametrize("method", METHODS)
def test_intraday_stop_before_limit_close_is_allowed(method):
    df = panel(opening=9.8, closing=9.0).with_columns(
        (pl.col("date") == date(2026, 7, 3)).alias("signal_limit_down"),
    )
    result = run(method, df, buy_day=0, sell_day=-1, stop_loss_pct=0.06)
    assert result.trades[0].exit_date == "2026-07-03"
    assert result.trades[0].exit_price == pytest.approx(9.4)


@pytest.mark.parametrize("method", METHODS)
def test_growth_board_ten_percent_opening_is_not_a_limit(method):
    result = run(method, panel(symbol="300001.SZ"))
    assert result.trades[0].entry_date == "2026-07-03"


@pytest.mark.parametrize("new_rule", [False, True])
def test_history_risk_flag_and_effective_rule_date(new_rule):
    df = panel(opening=10.5, closing=10.4).with_columns(
        pl.lit(True).alias("is_risk_warning"),
    )
    if new_rule:
        df = df.with_columns(pl.col("date") + pl.duration(days=5))
    result = run("simulate_portfolio", df)
    assert bool(result.trades) is new_rule


def test_registration_no_limit_window_is_not_blocked():
    df = panel().with_columns(pl.lit(date(2026, 7, 1)).alias("listing_date"))
    result = run("simulate_portfolio", df)
    assert result.trades[0].entry_date == "2026-07-03"


def test_etf_does_not_inherit_stock_opening_limits():
    result = run("simulate_portfolio", panel(), asset_type="etf")
    assert result.trades[0].entry_date == "2026-07-03"


def test_ex_right_reference_undoes_current_adjustment_scale():
    df = panel(scale=0.4).with_columns(
        pl.when(pl.col("date") < date(2026, 7, 3))
        .then(pl.col("raw_close") * 2).otherwise(pl.col("raw_close")).alias("raw_close"),
    )
    result = run("simulate_portfolio", df)
    assert not result.trades
    assert result.stats["execution"]["buy_limit_up"] == 1


def test_same_session_bars_keep_previous_session_reference():
    from datetime import datetime

    df = panel().head(3).with_columns(
        pl.Series("datetime", [
            datetime(2026, 7, 1, 15), datetime(2026, 7, 2, 9, 31),
            datetime(2026, 7, 2, 9, 32),
        ]),
    )
    market = build_market_data_matrix(df)
    assert market.field("execution_limit_up")[1, 0] == pytest.approx(10.995)
    assert market.field("execution_limit_up")[2, 0] == pytest.approx(10.995)


@pytest.mark.parametrize("as_of", ["2026-07-03", "2026-07-04", None])
def test_only_dated_authoritative_instrument_limit_applies(tmp_path, as_of):
    df = panel()
    root = tmp_path / "daily"
    for day, group in df.partition_by("date", as_dict=True).items():
        part = root / f"date={day[0]}"
        part.mkdir(parents=True)
        group.write_parquet(part / "data.parquet")
    market = load_market_data_matrix_from_parquet(
        root, date(2026, 7, 1), date(2026, 7, 5), field_columns=set(),
        instruments=pl.DataFrame({
            "symbol": ["600001.SH"], "limit_up": [12.0], "as_of": [as_of],
        }),
        cache_root=tmp_path / "cache",
    )
    expected = 11.995 if as_of == "2026-07-03" else 10.995
    assert market.field("execution_limit_up")[2, 0] == pytest.approx(expected)
    if as_of != "2026-07-03":
        corrected = load_market_data_matrix_from_parquet(
            root, date(2026, 7, 1), date(2026, 7, 5), field_columns=set(),
            instruments=pl.DataFrame({
                "symbol": ["600001.SH"], "limit_up": [12.0], "as_of": ["2026-07-03"],
            }),
            cache_root=tmp_path / "cache",
        )
        assert corrected.cache_path != market.cache_path
        assert corrected.field("execution_limit_up")[2, 0] == pytest.approx(11.995)


def test_close_sale_at_down_limit_uses_same_rule_in_portfolio():
    result = run(
        "simulate_portfolio", panel(opening=9.8, closing=9.0),
        buy_day=0, sell_day=2, exit_fill="close_t",
    )
    assert result.trades[0].exit_date == "2026-07-04"
    assert result.trades[0].blocked_exit_days == 1


@pytest.mark.parametrize("cached", [False, True])
def test_parquet_matrix_retains_limits_when_sliced_to_execution_window(tmp_path, cached):
    import numpy as np

    df = panel(scale=0.4)
    root = tmp_path / "daily"
    for day, group in df.partition_by("date", as_dict=True).items():
        part = root / f"date={day[0]}"
        part.mkdir(parents=True)
        group.write_parquet(part / "data.parquet")
    market = load_market_data_matrix_from_parquet(
        root, date(2026, 7, 2), date(2026, 7, 5),
        field_columns={"raw_close"}, cache_root=tmp_path / "cache" if cached else None,
    )
    entry = np.zeros(market.shape, dtype=np.uint8)
    entry[0, 0] = 1
    matrix = build_market_matrix_from_signals(
        market, make_signal_matrix(market.shape, entry=entry), entry_delay_bars=1,
    )
    result = BacktestEngine(None).simulate_market_matrix(
        matrix, MatcherConfig(matching="open_t+1"),
    )
    assert not result.trades
    assert result.stats["execution"]["buy_limit_up"] == 1


@pytest.mark.parametrize("method", METHODS)
def test_gap_stop_records_one_blocked_day(method):
    result = run(
        method, panel(opening=9, closing=9.5), buy_day=0, sell_day=-1,
        stop_loss_pct=0.06,
    )
    assert result.trades[0].blocked_exit_days == 1
    assert result.stats["execution"]["sell_limit_down"] == 1


@pytest.mark.parametrize("method", METHODS)
def test_missing_next_session_expires_buy_instead_of_filling_on_resume(method):
    df = pl.concat([
        panel().filter(pl.col("date") != date(2026, 7, 3)),
        panel(symbol="600002.SH"),
    ]).sort(["symbol", "date"])
    entry = (df["symbol"] == "600001.SH") & (df["date"] == date(2026, 7, 2))
    result = getattr(BacktestEngine(None), method)(
        df, entry, pl.Series([False] * len(df)),
        MatcherConfig(matching="open_t+1", fees_pct=0, slippage_bps=0),
    )
    assert not result.trades
    assert result.stats["execution"]["buy_expired"] == 1


@pytest.mark.parametrize("method", METHODS)
def test_unfillable_tail_is_not_a_realized_trade(method):
    df = panel(opening=9, closing=9).head(3)
    result = run(method, df, buy_day=0, sell_day=1)
    assert not result.trades
    assert result.stats["pending_exit_positions"] == 1
    if "independent" in method:
        assert result.stats["unclosed_candidates"] == 1
        assert result.stats["unrealized_pnl_amount"] == pytest.approx(-100)
