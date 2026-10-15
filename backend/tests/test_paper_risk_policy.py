"""Entry policy recovery, deterministic allocation and published opening prices."""
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from test_paper_auto import DAY, OTHER, SYM, event, make_rule
from test_paper_auto import account as account

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto


def test_policy_price_lines_gap_and_prior_peak():
    from app.strategy.risk_policy import RiskPolicy, price_exit
    policy = RiskPolicy(stop_loss_pct=.06, trailing_stop_pct=.1,
                        trailing_take_profit_activate_pct=.15,
                        trailing_take_profit_drawdown_pct=.05, take_profit_pct=.3)
    assert price_exit(policy, 10, 12, 11, 10, 15) == ("trailing_take_profit", 11)
    assert price_exit(policy, 10, 10, 10, 9.5, 12) == (None, None)
    assert price_exit(policy, 10, 10, 14, 14, 14) == ("take_profit", 14)


def test_effective_policy_respects_explicit_disable():
    from app.strategy.risk_policy import strategy_policy
    strategy = SimpleNamespace(stop_loss=-.06, take_profit=.15, max_hold_days=15)
    policy = strategy_policy(strategy, {"stop_loss": None, "take_profit": .001})
    assert policy.stop_loss_pct is None
    assert policy.take_profit_pct == .01
    assert policy.max_hold_days == 15


def test_rank_scores_then_symbol_and_missing_last(account):
    make_rule(account, max_positions=2, candidate_priority="score")
    orders = paper_auto.on_rule_events(account, [
        event("buy_signal", SYM, score=None),
        event("buy_signal", "600000.SH", score=80),
        event("buy_signal", OTHER, score=80),
    ])
    assert [order["symbol"] for order in orders] == [OTHER, "600000.SH"]


def test_buy_signal_and_pool_entry_allocate_one_candidate_per_rule(account):
    make_rule(account, cooldown_days=0)
    orders = paper_auto.on_rule_events(account, [
        event("buy_signal"), event("pool_entry"),
    ])
    assert len(orders) == 1 and orders[0]["qty"] == 300


def test_pending_cash_and_exposure_include_manual_buys_and_additions(account):
    make_rule(account, cooldown_days=0, max_positions=0, size_value=60000,
              max_symbol_exposure_pct=25, max_total_exposure_pct=30)
    pending, error = paper.create_order(account, SYM, "buy", qty=2000, ref_price=10)
    assert error is None
    orders = paper_auto.on_rule_events(account, [
        event("buy_signal", SYM), event("buy_signal", OTHER),
    ])
    assert [order["qty"] for order in orders] == [400, 500]
    assert paper.get_order(account, pending["id"])["status"] == "pending"
    # All pending buys reserve fees as well as notional, even across symbols.
    assert paper.create_order(account, "600000.SH", "buy", qty=7200, ref_price=10)[1]


def test_entry_snapshot_is_ledger_owned_and_risk_survives_t1_restart(account, monkeypatch):
    rule = make_rule(account, risk_follow=True, cooldown_days=0)
    snapshot = {"version": 1, "strategy_id": "strategy-one",
                "policy": {"stop_loss_pct": .06, "max_hold_days": 15}}
    order = paper_auto.on_rule_events(account, [event("buy_signal", entry_policy=snapshot)])[0]
    snapshot["policy"]["stop_loss_pct"] = .5
    paper.evaluate_intraday(account, {SYM: 10})
    assert order["entry_policy"]["policy"]["stop_loss_pct"] == .06
    assert paper.load_fills(account)[0]["entry_policy"] == order["entry_policy"]
    paper.rebuild_positions(account)
    assert paper.load_positions(account)[SYM]["entry_policy"] == order["entry_policy"]
    # A stop is latched during T+1 and remains due even if the price recovers.
    assert paper_auto.evaluate_position_risk(account, {SYM: 9}) == []
    assert paper_auto.load_exit_intents(account)[0]["risk_reason"] == "stop_loss"
    paper_auto.save_auto_rule(account, {**rule, "size_value": 1000})
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    restarted = Path(str(account))
    sells = paper_auto.on_rule_events(restarted, [], prices={SYM: 10})
    assert len(sells) == 1 and sells[0]["qty"] == 300
    paper.evaluate_intraday(restarted, {SYM: 10})
    assert paper.load_positions(restarted)[SYM]["qty"] == 0


def test_legacy_hold_is_not_assigned_current_strategy_policy(account):
    make_rule(account, risk_follow=True, cooldown_days=0)
    paper.create_order(account, SYM, "buy", qty=100, ref_price=10)
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper_auto.evaluate_position_risk(account, {SYM: 1}) == []
    assert paper_auto.load_exit_intents(account) == []


def test_raw_open_matches_only_fresh_print_after_placement(account, monkeypatch):
    stamp = datetime.combine(DAY, time(9, 31), CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(DAY, time(8), CN_TZ))
    order, _ = paper.create_order(account, SYM, "buy", qty=100, ref_price=10, order_type="next_open")
    monkeypatch.setattr(paper, "cn_now", lambda: stamp)
    old = {SYM: {"date": DAY, "raw_open": 10.1, "quote_ts": stamp.timestamp() - 60}}
    assert paper.evaluate_opening(account, old) == []
    missing = {SYM: {"date": DAY, "open": 5.05, "quote_ts": stamp.timestamp()}}
    assert paper.evaluate_opening(account, missing) == []
    fresh = {SYM: {"date": DAY, "raw_open": 10.1, "quote_ts": stamp.timestamp()}}
    assert len(paper.evaluate_opening(account, fresh)) == 1
    assert paper.get_order(account, order["id"])["fill_price"] == pytest.approx(10.105)
    assert paper.evaluate_opening(account, fresh) == []
    assert paper._available_of(paper.load_positions(account)[SYM], DAY.isoformat()) == 0
    late, _ = paper.create_order(account, OTHER, "buy", qty=100, ref_price=10, order_type="next_open")
    assert paper.evaluate_opening(account, {OTHER: {**fresh[SYM], "raw_open": 10}}) == []
    assert paper.get_order(account, late["id"])["status"] == "pending"


def test_execution_rechecks_exposure_after_price_moves(account):
    make_rule(account, size_value=24900, max_symbol_exposure_pct=25, max_positions=0)
    buy = paper_auto.on_rule_events(account, [event("buy_signal")])[0]
    assert buy["qty"] == 2400
    assert paper.evaluate_intraday(account, {SYM: 10.8}) == []
    assert "仓位上限" in paper.get_order(account, buy["id"])["reason"]
    assert paper.load_fills(account) == []


def test_trailing_peak_ignores_stale_quotes_and_survives_restart(account, monkeypatch):
    make_rule(account, risk_follow=True, cooldown_days=0)
    paper_auto.on_rule_events(account, [event("buy_signal", entry_policy={
        "version": 1, "policy": {"trailing_take_profit_activate_pct": .15,
                               "trailing_take_profit_drawdown_pct": .05},
    })])
    paper.evaluate_intraday(account, {SYM: 10})
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    now = paper.cn_now().timestamp()
    assert paper_auto.evaluate_position_risk(account, {SYM: 15}, quote_times={SYM: now - 60}) == []
    assert paper_auto.evaluate_position_risk(account, {SYM: 11.8}, quote_times={SYM: now}) == []
    assert paper_auto.evaluate_position_risk(Path(str(account)), {SYM: 11.1},
                                            quote_times={SYM: now + 1})[0]["side"] == "sell"
    assert paper_auto.load_exit_intents(account)[0]["risk_reason"] == "trailing_take_profit"


def test_max_hold_uses_published_trading_days_over_national_holiday(account, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY.replace(month=9, day=30))
    make_rule(account, risk_follow=True, cooldown_days=0)
    paper_auto.on_rule_events(account, [event("buy_signal", entry_policy={
        "version": 1, "policy": {"max_hold_days": 2},
    })])
    paper.evaluate_intraday(account, {SYM: 10})
    monkeypatch.setattr(paper, "cn_today", lambda: DAY.replace(day=8))
    assert paper_auto.evaluate_position_risk(account, {SYM: 10}) == []
    monkeypatch.setattr(paper, "cn_today", lambda: DAY.replace(day=9))
    sells = paper_auto.evaluate_position_risk(account, {SYM: 10})
    assert len(sells) == 1
    assert paper_auto.load_exit_intents(account)[0]["risk_reason"] == "max_hold"


def test_additions_keep_first_fill_policy_and_reentry_uses_new_policy(account, monkeypatch):
    make_rule(account, risk_follow=True, cooldown_days=0)
    def entry(stop):
        return event("buy_signal", entry_policy={"version": 1, "policy": {"stop_loss_pct": stop}})
    paper_auto.on_rule_events(account, [entry(.06)])
    paper.evaluate_intraday(account, {SYM: 10})
    paper_auto.on_rule_events(account, [entry(.5)])
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper.load_positions(account)[SYM]["entry_policy"]["policy"]["stop_loss_pct"] == .06
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    paper_auto.evaluate_position_risk(account, {SYM: 9})
    paper.evaluate_intraday(account, {SYM: 9})
    paper_auto.on_rule_events(account, [entry(.5)])
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper.load_positions(account)[SYM]["entry_policy"]["policy"]["stop_loss_pct"] == .5


def test_new_raw_open_column_survives_mixed_parquet_versions(tmp_path):
    from app.parquet import scan_enriched_parquet
    old, new = tmp_path / "old.parquet", tmp_path / "new.parquet"
    pl.DataFrame({"symbol": [SYM], "date": [DAY], "open": [5.0]}).write_parquet(old)
    pl.DataFrame({"symbol": [SYM], "date": [DAY + timedelta(days=1)],
                  "open": [5.1], "raw_open": [10.2], "quote_time_basis": ["source_snapshot"]}).write_parquet(new)
    rows = scan_enriched_parquet(str(tmp_path / "*.parquet")).sort("date").collect()
    assert rows["raw_open"].to_list() == [None, 10.2]
    assert rows["quote_time_basis"].to_list() == [None, "source_snapshot"]


def test_raw_open_is_preserved_before_full_and_realtime_adjustment():
    from test_realtime_enriched_resume import _live_state, _previous_enriched

    from app.indicators.pipeline import compute_enriched, compute_enriched_today
    symbol = "600001.SH"
    raw = pl.DataFrame({
        "symbol": [symbol, symbol], "date": [DAY, DAY + timedelta(days=1)],
        "open": [20., 10.], "high": [21., 11.], "low": [19., 9.], "close": [20., 10.],
        "volume": [1000., 1000.], "amount": [20000., 10000.],
    })
    factors = pl.DataFrame({"symbol": [symbol], "trade_date": [DAY + timedelta(days=1)], "ex_factor": [2.]})
    full = compute_enriched(raw, factors)
    assert full["raw_open"].to_list() == [20., 10.]
    assert full["open"].to_list() == [10., 10.]
    live = compute_enriched_today(
        _live_state().with_columns(pl.lit(.5).alias("_adj_factor")),
        _previous_enriched(), raw.head(1), None,
    )
    assert live["raw_open"].item() == 20
    assert live["open"].item() == 10


def test_disabling_risk_cancels_pending_sell_and_reenable_creates_new_intent(account, monkeypatch):
    rule = make_rule(account, risk_follow=True, cooldown_days=0)
    paper_auto.on_rule_events(account, [event("buy_signal", entry_policy={
        "version": 1, "policy": {"stop_loss_pct": .06},
    })])
    paper.evaluate_intraday(account, {SYM: 10})
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    first = paper_auto.evaluate_position_risk(account, {SYM: 9})[0]
    old_id = first["exit_intent_id"]
    paper_auto.save_auto_rule(account, {**rule, "risk_follow": False})
    assert paper.get_order(account, first["id"])["status"] == "cancelled"
    assert paper_auto.load_exit_intents(account, active_only=True) == []
    paper_auto.save_auto_rule(account, rule)
    second = paper_auto.evaluate_position_risk(account, {SYM: 9})[0]
    assert second["exit_intent_id"] != old_id
    assert len(paper_auto.load_exit_intents(account)) == 2
