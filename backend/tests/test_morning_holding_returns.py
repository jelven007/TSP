"""Holding exits use their frozen entry reference independently of yesterday's close."""
from datetime import timedelta

import polars as pl
import pytest
from test_morning_liquidation_returns import AT, DAY, frame, position, run

from app.market_time import CN_TZ
from app.strategy import custom_signals, paper
from app.strategy.morning_liquidation import (
    evaluate,
    holding_snapshot,
    parameters,
    requires_previous_close,
)


def bracket(**patch):
    return {
        "id": "morning_liquidation_s03", "name": "s03_早盘清仓", "kind": "exit",
        "timeframe": "intraday", "template": "morning_liquidation", "conditions": [],
        "enabled": True, "params": dict.fromkeys((
            "start_time", "rise_pct", "fall_pct", "decline_3m_pct", "volume_ratio_min",
            "take_profit_pct", "stop_loss_pct", "time", "fallback_time",
        )) | patch,
    }


@pytest.mark.parametrize("key,price,reason", [
    ("take_profit_pct", 10.1, "take_profit"), ("stop_loss_pct", 9.4, "stop_loss"),
])
def test_holding_thresholds_use_entry_price_instead_of_previous_close(key, price, reason):
    hit, = run(frame(price), sig=bracket(**{key: 1 if reason == "take_profit" else 6}),
               positions=[position(entry_price=10)], prev_close={"a": 20}).hits
    assert hit["reason"] == reason
    assert hit["entry_price"] == 10
    assert hit["entry_price_basis"] == "filled_average"
    assert hit["pnl_pct"] == pytest.approx(.01 if reason == "take_profit" else -.06)


def test_blank_brackets_are_saved_as_null_and_do_not_trigger(tmp_path):
    sig = bracket()
    custom_signals.save_one(tmp_path, sig)
    assert custom_signals.load_all(tmp_path) == [sig]
    assert parameters(sig) == sig["params"]
    assert not run(frame(100), sig=sig, positions=[position(entry_price=1)], prev_close={}).hits


@pytest.mark.parametrize("key", ["take_profit_pct", "stop_loss_pct"])
@pytest.mark.parametrize("value", [0, .09, 100.1, True, float("nan"), "1", ""])
def test_brackets_reject_invalid_percentage_values(key, value):
    with pytest.raises(ValueError):
        parameters(bracket(**{key: value}))


@pytest.mark.parametrize("key", ["take_profit_pct", "stop_loss_pct"])
@pytest.mark.parametrize("value", [.1, 100, None])
def test_brackets_allow_inclusive_boundaries_and_disabled_values(key, value):
    assert parameters(bracket(**{key: value}))[key] == value


def test_live_bracket_needs_a_fresh_quote_but_not_previous_close():
    quote = pl.DataFrame({"symbol": ["a"], "price": [10.1],
                          "quote_ts": [AT.replace(tzinfo=CN_TZ).timestamp()]})
    hit, = run(pl.DataFrame(), sig=bracket(take_profit_pct=1),
               positions=[position(entry_price=10)], quotes=quote, prev_close={}).hits
    assert hit["reason"] == "take_profit" and hit["source"] == "live_quote"
    assert not run(pl.DataFrame(), sig=bracket(take_profit_pct=1),
                   positions=[position(entry_price=10)], quotes=quote, prev_close={},
                   now=AT + timedelta(seconds=16)).hits


def test_bracket_keeps_first_completed_trigger_and_obeys_start_and_t_plus_one():
    sig = bracket(take_profit_pct=1, stop_loss_pct=6, start_time="10:00:00")
    data = pl.concat([frame(10.1), frame(9.4, at=AT + timedelta(minutes=1))])
    hit, = run(data, sig=sig, positions=[position(entry_price=10)],
               now=AT + timedelta(minutes=1)).hits
    assert hit["reason"] == "take_profit" and hit["trigger_time"] == AT.isoformat()
    assert not run(data, sig=sig, positions=[position(entry_price=10)],
                   now=AT - timedelta(seconds=1)).hits
    assert not run(data, sig=sig, positions=[position(entry_price=10, entry_date=str(DAY))]).hits


def test_missing_cost_reports_a_diagnostic_but_preserves_enabled_clock():
    result = run(frame(100), sig=bracket(take_profit_pct=1, time="10:30:00"),
                 positions=[position(entry_price=None)], now=AT.replace(minute=30))
    assert result.hits[0]["reason"] == "stop_time"
    assert any("买入" in item and "a" in item for item in result.diagnostics)


def test_signal_only_roster_uses_frozen_raw_buy_reference_and_labels_its_basis():
    result = evaluate(frame(10.1), bracket(take_profit_pct=1), day=DAY, now=AT,
                      positions=None, prev_close={},
                      roster=[{"symbol": "a", "entry_date": "2026-09-04",
                               "raw_close": 10., "close": 5.}])
    hit, = result.hits
    assert hit["reason"] == "take_profit"
    assert hit["entry_price"] == 10
    assert hit["entry_price_basis"] == "buy_signal_reference"


def test_actual_snapshot_weights_remaining_fifo_buy_prices_without_fees(tmp_path):
    paper.create_account(tmp_path, 10000, account_id="one", name="test")
    for index, (qty, price) in enumerate([(100, 10), (300, 12)]):
        paper._append_fill(tmp_path, {"symbol": "a", "side": "buy", "qty": qty,
                                    "price": price, "fee": 10, "date": "2026-09-04",
                                    "order_id": str(index),
                                    "entry_policy": {"strategy_id": "sample"}}, "one")
    row, = holding_snapshot(tmp_path, "sample", DAY)
    assert row["entry_price"] == 11.5
    paper._append_fill(tmp_path, {"symbol": "a", "side": "sell", "qty": 100,
                                "price": 11, "fee": 0, "date": str(DAY), "order_id": "sell"}, "one")
    row, = holding_snapshot(tmp_path, "sample", DAY)
    assert row["entry_price"] == 12 and row["qty"] == 300


def test_brackets_do_not_require_previous_close_or_change_legacy_defaults():
    assert not requires_previous_close(bracket(take_profit_pct=1))
    assert requires_previous_close(bracket(take_profit_pct=1, rise_pct=6))
    assert "take_profit_pct" not in parameters({"params": {"time": "10:30:00"}})
    with pytest.raises(ValueError):
        parameters({"params": {"amplitude_pct": 6, "take_profit_pct": None}})


def test_signal_sell_task_can_use_buy_reference_without_loading_previous_close(monkeypatch):
    from types import SimpleNamespace

    from app.services.strategy_monitor_tasks import evaluate_sell_task
    from app.strategy import late_session_strength

    def unexpected(*args, **kwargs):
        raise AssertionError("Cost-only exit must not load previous closes")

    monkeypatch.setattr(late_session_strength, "previous_closes", unexpected)
    context = SimpleNamespace(observed_at=AT, as_of=DAY, exit_errors=[], history=frame(10.1),
                              current=pl.DataFrame(), suspension_history=None)
    windows = {"sell": [{"start": "09:30:00", "end": "15:00:00",
                         "signal": bracket(take_profit_pct=1)}]}
    roster = {"rows": [{"symbol": "a", "entry_date": "2026-09-04", "raw_close": 10.}]}
    result = evaluate_sell_task(None, None, context, {}, windows, roster)
    assert result["state"] == "success" and result["hits"][0]["reason"] == "take_profit"


@pytest.mark.parametrize("event", [False, True])
def test_corporate_action_does_not_leave_a_stale_fifo_entry_reference(tmp_path, event):
    paper.create_account(tmp_path, 10000, account_id="one", name="test")
    fills = [{"symbol": "a", "side": "buy", "qty": 100, "price": 10, "fee": 0,
              "date": "2026-09-04", "order_id": "buy", "entry_policy": {"strategy_id": "sample"}}]
    fills.append({"symbol": "a", "kind": "corporate_action", "stage": "ex",
                  "price_factor": 2, "qty": 100, "asset_type": "stock", "date": str(DAY),
                  "available_on": str(DAY), "action_id": "event"} if event else
                 {"symbol": "a", "kind": "corp_action", "factor": 2, "date": str(DAY)})
    positions, _ = paper.replay_positions(tmp_path, fills, "one")
    lots = positions["a"]["lots"]
    assert all(lot.get("entry_price") is None for lot in lots) if event else lots[0]["entry_price"] == 5
