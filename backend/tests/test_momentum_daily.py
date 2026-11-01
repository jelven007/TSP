from dataclasses import replace
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext

DAY = date(2026, 8, 25)


def minutes(symbol="600001.SH", day=DAY, end=900):
    labels = list(range(571, 691)) + list(range(781, end + 1))
    prices = [10.1 + (b - 831) * .001 if b >= 831 else 10.1 for b in labels]
    return pl.DataFrame({
        "symbol": [symbol] * len(labels),
        "datetime": [datetime.combine(day, datetime.min.time()) + timedelta(minutes=b) for b in labels],
        "open": prices, "high": [p + .01 for p in prices], "low": [p - .01 for p in prices],
        "close": prices, "volume": [1000.] * len(labels), "amount": [1e6] * len(labels),
    })


def history():
    from app.market_time import published_trading_days
    days = sorted(d for d in published_trading_days(2026) if d < DAY)[-25:]
    return pl.DataFrame({
        "symbol": ["600001.SH"] * len(days), "date": days,
        "raw_close": [9.9] * (len(days) - 1) + [10.],
        "close": [9.9] * (len(days) - 1) + [10.], "amount": [2e8] * len(days),
        "float_shares": [1e9] * len(days), "adj_factor": [1.] * len(days),
    })


def test_all_selection_causal_and_ranked_candidates_are_separate():
    from app.strategy.momentum_selection import ranked_candidates, select_entries

    base, daily = minutes(), history()
    members = [(f"60000{i}.SH", None) for i in range(1, 8)]
    frame = pl.concat([base.with_columns(pl.lit(s).alias("symbol")) for s, _ in members])
    panel = pl.concat([daily.with_columns(pl.lit(s).alias("symbol")) for s, _ in members])
    chosen = select_entries(frame, {}, daily=panel, members=members)
    assert chosen.height == 7
    assert ranked_candidates(chosen, {}).height == 5
    # Post-signal prices and amounts cannot change selection.
    altered = frame.with_columns(pl.when(pl.col("datetime").dt.time() > datetime(2026, 1, 1, 14, 30).time())
                                .then(200.).otherwise(pl.col("close")).alias("close"))
    assert select_entries(altered, {}, daily=panel, members=members).equals(chosen)
    assert select_entries(frame.filter(pl.col("datetime").dt.hour() < 14), {},
                          daily=panel, members=members).is_empty()


def test_missing_five_minute_window_and_future_listing_excluded():
    from app.strategy.momentum_selection import select_entries

    frame = minutes().filter(pl.col("datetime") != datetime(2026, 8, 25, 14, 0))
    assert select_entries(frame, {}, daily=history(), members=[("600001.SH", None)]).is_empty()
    assert select_entries(minutes(), {}, daily=history(),
                          members=[("600001.SH", "2026-08-26")]).is_empty()


def test_net_stop_precedes_gross_target_and_s01_guard_stays_strict():
    from app.strategy.builtin._overnight import parameters as overnight
    from app.strategy.momentum_rules import exit_quote, net_proceeds, parameters, stop_quote

    p = parameters({"stop_loss_pct": .06, "take_profit_pct": .06})
    pos = {"shares": 1000, "remaining": 1000, "entry_notional": 10000., "entry_value": 10005.}
    quote = stop_quote(pos, p)
    assert net_proceeds(quote, pos, p) >= pos["entry_value"] * .94
    assert net_proceeds(quote - .01, pos, p) < pos["entry_value"] * .94
    reason, _ = exit_quote({"bar": 600, "open": 10., "high": 10.7, "low": 9., "close": 10.}, pos, p)
    assert reason == "net_stop"
    with pytest.raises(ValueError, match="11:15"):
        overnight({"guard_time": "10:30"})


def test_day_empty_sections_and_t1_and_deadline_failure():
    from app.strategy.momentum_day import run_day

    context = StrategyDataContext("stock", "1m", DAY, history=minutes(), daily_history=history())
    empty = pl.DataFrame()
    result = run_day(context, {}, empty, previous_state={"cash": 100000., "positions": [], "as_of": "2026-08-24"})
    assert set(result["sections"]) == {"sell", "select", "buy"}
    assert result["sections"] == {"sell": [], "select": [], "buy": []}
    pos = {"symbol": "600001.SH", "entry_date": str(DAY), "sell_date": "2026-08-26",
           "shares": 1000, "remaining": 1000, "entry_notional": 10000., "entry_value": 10005.,
           "last_mark": 10., "score": 0., "entry_nav": 100000., "entry_budget": 20000.}
    state = {"cash": 89995., "positions": [pos], "as_of": "2026-08-24"}
    assert not run_day(context, {}, empty, previous_state=state)["sections"]["sell"]
    zero = minutes(day=date(2026, 8, 26)).with_columns(pl.lit(0.).alias("volume"))
    later = replace(context, as_of=date(2026, 8, 26), history=zero)
    result = run_day(later, {}, empty, previous_state=state)
    assert result["error"] and "到期剩余" in result["error"]
    assert result["state"]["positions"][0]["remaining"] == 1000
    assert result["state"]["cash"] == 89995.
    assert not result["trades"]


def test_daily_identity_one_record_with_late_run_fence(tmp_path):
    from app.services.strategy_results import ResultIdentity, StrategyResults

    identity = ResultIdentity("s02", "s02", "v1", "{}", "{}", "null", "stock", "1m",
                              daily_record=True)
    changed = replace(identity, params_json='{"take_profit_pct":0.06}', strategy_version="v2")
    assert identity.record_id(str(DAY)) == changed.record_id(str(DAY))
    store = StrategyResults(tmp_path)
    result = {"as_of": str(DAY), "rows": [], "total": 0,
              "sections": {"sell": [], "select": [], "buy": []}}
    store.save(identity, result, started_at=2)
    store.save(changed, {**result, "total": 3}, started_at=3)
    store.save(identity, result, started_at=1)
    assert store.read(changed, str(DAY))["result"]["total"] == 3
    assert len(store.list_history("s02")["records"]) == 1
    assert StrategyResults(tmp_path).read(identity, str(DAY))["result"]["sections"] == result["sections"]


def test_resumed_trade_counts_actual_holding_sessions():
    from app.strategy.momentum_day import run_day
    resumed = date(2026, 9, 2)
    pos = {"symbol": "600001.SH", "entry_date": "2026-08-25", "sell_date": "2026-08-26",
           "shares": 1000, "remaining": 1000, "entry_notional": 10000., "entry_value": 10005.,
           "last_mark": 10., "score": 0., "entry_nav": 100000., "entry_budget": 20000.,
           "suspended": True}
    context = StrategyDataContext("stock", "1m", resumed, history=minutes(day=resumed), daily_history=history())
    result = run_day(context, {}, pl.DataFrame(), previous_state={"cash": 89995., "positions": [pos]})
    assert result["error"] is None
    assert result["trades"][0]["duration"] == 6


def test_intraday_context_uses_todays_announced_shares_and_ex_reference(tmp_path):
    from types import SimpleNamespace

    from app.backtest.momentum_run import prepare_daily
    shares = tmp_path / "financials" / "shares"
    shares.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600001.SH"], "period_end": [date(2026, 6, 30)],
                  "announce_date": [DAY], "float_shares": [2e9]}).write_parquet(shares / "part.parquet")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        execute_all=lambda *args: [("600001.SH", DAY, 2.)],
        get_instruments_asset=lambda *_: pl.DataFrame({"symbol": ["600001.SH"], "float_shares": [1e9]}))
    prepared = prepare_daily(history(), repo, as_of=DAY)
    current = prepared.filter(pl.col("date") == DAY).row(0, named=True)
    assert current["reference_close"] == 5.
    assert current["float_shares"] == 2e9
    assert current["close"] is None and current["amount"] is None


def test_daily_state_preserves_verified_suspension_evidence():
    from app.strategy.momentum_day import run_day

    pos = {"symbol": "600001.SH", "entry_date": "2026-08-24", "sell_date": str(DAY),
           "shares": 1000, "remaining": 1000, "entry_notional": 10000., "entry_value": 10005.,
           "last_mark": 10., "entry_nav": 100000., "entry_budget": 20000.}
    state = {"cash": 89995., "positions": [pos], "suspension_policy": "wait_resume",
             "suspension_events": [{"symbol": "600001.SH", "date": str(DAY),
                 "available_date": str(DAY), "suspended": True, "source": "已公告停牌证据"}]}
    context = StrategyDataContext("stock", "1m", DAY, history=pl.DataFrame(), daily_history=history())
    result = run_day(context, {}, pl.DataFrame(), previous_state=state)
    assert result["error"] is None
    assert result["sections"]["sell"][0]["status"] == "停牌等待复牌"
