"""S02 paper execution uses the same capacity and cost contracts as replay."""
from datetime import date, datetime, time, timedelta

import polars as pl
import pytest

from app.backtest.entry_execution import execute_entry_window
from app.market_time import CN_TZ
from app.strategy import paper, paper_auto

DAY = date(2026, 10, 14)
SYM = "600001.SH"
S02 = "custom_mvxxcp88"


class Repo:
    def __init__(self):
        self.frame = pl.DataFrame()

    def get_minute_batch(self, symbols, day, asset_type="stock"):
        return self.frame

    def get_daily_close_inputs(self, day, *, include_closing_minute=True):
        return pl.DataFrame({"symbol": [SYM], "date": [day], "close": [10.]}), pl.DataFrame()

    def get_suspension_history(self):
        return pl.DataFrame()


def bars(start, n=5, *, high=10.2, close=10., volume=1.):
    return pl.DataFrame({
        "symbol": [SYM] * n,
        "datetime": [start + timedelta(minutes=i) for i in range(n)],
        "open": [10.] * n, "high": [high] * n, "low": [10.] * n,
        "close": [close] * n, "volume": [volume] * n,
    })


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [datetime.combine(DAY, time(14, 30, 30), CN_TZ)]
    monkeypatch.setattr(paper, "cn_now", lambda: clock[0])
    monkeypatch.setattr(paper, "cn_today", lambda: clock[0].date())
    monkeypatch.setattr(paper_auto, "cn_now", lambda: clock[0])
    paper.create_account(tmp_path, 1_000_000)
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "r_s02", "match_kind": "strategy", "match_id": S02,
        "side": "auto", "size_mode": "pct_equity", "size_value": 20.,
        "order_type": "market", "execution_mode": "s02_minute",
        "candidate_priority": "score", "cooldown_days": 0, "max_positions": 5,
        "max_total_exposure_pct": 100., "max_symbol_exposure_pct": 20.,
        "risk_follow": True,
    })
    repo = Repo()
    event = {
        "id": "entry-one", "source": "strategy", "strategy_id": S02,
        "type": "buy_signal", "symbol": SYM, "price": 10., "score": 100.,
        "entry_policy": {
            "version": 1, "strategy_id": S02, "as_of": str(DAY),
            "policy": {}, "entry_risk": {},
            "signal_datetime": datetime.combine(DAY, time(14, 30)).isoformat(),
            "entry_evaluated_at": datetime.combine(DAY, time(14, 30, 30)).isoformat(),
            "exit_definition": {"id": "morning_liquidation", "template": "morning_liquidation",
                                "enabled": True, "params": {
                                    "rise_pct": 6., "fall_pct": 6.,
                                    "time": "10:30:00", "fallback_time": "10:45:00"}},
        },
    }
    return tmp_path, clock, rule, repo, event


def entry(setup):
    storage, clock, _rule, repo, event = setup
    return paper_auto.on_rule_events(storage, [event], minute_repo=repo,
                                     completed_through=clock[0])[0]


def advance(setup, at):
    storage, clock, _rule, repo, _event = setup
    clock[0] = at.replace(tzinfo=CN_TZ)
    paper_auto.on_rule_events(storage, [], minute_repo=repo, completed_through=clock[0])


def test_buy_matches_shared_window_fees_and_survives_replay(setup):
    from app.strategy.paper_minute import matcher_for_account

    storage, _clock, _rule, repo, _event = setup
    order = entry(setup)
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start)
    matcher = matcher_for_account(paper.get_account(storage))
    expected = execute_entry_window(
        repo.frame, symbol=SYM, start_at=start, prior_close=10., budget=200_000.,
        size_order=lambda price: matcher.buy_quantity(200_000., price, SYM),
        buy_value=matcher.buy_value,
    )
    advance(setup, start)
    assert len(paper.load_fills(storage)) == 1  # future bars cannot be used
    advance(setup, start)
    assert len(paper.load_fills(storage)) == 1
    advance(setup, start + timedelta(minutes=4))
    fills = paper.load_fills(storage)
    assert [f["qty"] for f in fills] == [f.shares for f in expected.fills]
    assert [f["price"] for f in fills] == [f.price for f in expected.fills]
    assert sum(f["fee"] for f in fills) == pytest.approx(expected.fee)
    assert paper.get_account(storage)["cash"] == pytest.approx(1_000_000. - expected.value, abs=.01)
    saved = paper.get_order(storage, order["id"])
    assert saved["status"] == "cancelled" and saved["filled_qty"] == 500
    assert paper.load_positions(storage)[SYM]["qty"] == 500
    advance(setup, start + timedelta(minutes=5))
    assert len(paper.load_fills(storage)) == 5


def test_minute_orders_cannot_be_consumed_by_snapshot_or_settlement(setup, monkeypatch):
    storage, _clock, _rule, _repo, _event = setup
    order = entry(setup)
    assert paper.evaluate_intraday(storage, {SYM: 10.}) == []
    monkeypatch.setattr(paper, "read_daily_bar", lambda *a: {"open": 10., "close": 10.})
    paper.settle_day(storage, str(DAY))
    assert paper.get_order(storage, order["id"])["status"] == "pending"
    assert paper.load_fills(storage) == []


@pytest.mark.parametrize("action", ["cancel", "disable", "freeze", "delete", "change"])
def test_interleaving_stops_new_fills(setup, action):
    storage, _clock, rule, repo, _event = setup
    order = entry(setup)
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start)
    advance(setup, start)
    if action == "cancel":
        paper.cancel_order(storage, order["id"])
    elif action == "disable":
        paper_auto.set_enabled(storage, rule["id"], False)
    elif action == "freeze":
        paper.set_frozen(storage, frozen=True)
    elif action == "delete":
        paper_auto.delete_auto_rule(storage, rule["id"])
    else:
        paper_auto.update_auto_rule(storage, rule["id"], {"execution_mode": "orders"})
    advance(setup, start + timedelta(minutes=4))
    assert len(paper.load_fills(storage)) == 1


def test_late_entry_never_backfills_printed_minutes_or_extends_window(setup):
    storage, clock, _rule, repo, _event = setup
    clock[0] = datetime.combine(DAY, time(14, 33, 30), CN_TZ)
    order = entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)))
    advance(setup, datetime.combine(DAY, time(14, 35)))
    assert [f["minute_datetime"] for f in paper.load_fills(storage)] == [
        datetime.combine(DAY, time(14, 34)).isoformat(),
        datetime.combine(DAY, time(14, 35)).isoformat(),
    ]
    assert paper.get_order(storage, order["id"])["status"] == "cancelled"


def test_expired_signal_and_pool_entry_do_not_create_buy(setup):
    storage, clock, _rule, repo, event = setup
    clock[0] = datetime.combine(DAY, time(14, 36), CN_TZ)
    assert paper_auto.on_rule_events(storage, [event], minute_repo=repo,
                                     completed_through=clock[0]) == []
    clock[0] = datetime.combine(DAY, time(14, 30, 30), CN_TZ)
    assert paper_auto.on_rule_events(storage, [{**event, "type": "pool_entry"}],
                                     minute_repo=repo, completed_through=clock[0]) == []


def test_t1_exit_at_s02_trigger_is_capacity_limited_and_fee_aggregated(setup):
    from app.strategy.paper_minute import matcher_for_account

    storage, _clock, _rule, repo, _event = setup
    entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)))
    advance(setup, datetime.combine(DAY, time(14, 35)))
    assert paper.load_positions(storage)[SYM]["available_qty"] == 0
    next_day = DAY + timedelta(days=1)
    repo.frame = bars(datetime.combine(next_day, time(9, 31)), n=5, close=10.6)
    advance(setup, datetime.combine(next_day, time(9, 35)))
    sells = [f for f in paper.load_fills(storage) if f["side"] == "sell"]
    assert len(sells) == 1, (paper_auto.load_auto_skips(storage), paper.load_orders(storage))
    repo.frame = bars(datetime.combine(next_day, time(9, 31)), n=9, close=10.6)
    advance(setup, datetime.combine(next_day, time(9, 39)))
    sells = [f for f in paper.load_fills(storage) if f["side"] == "sell"]
    assert sum(f["qty"] for f in sells) == 500
    matcher = matcher_for_account(paper.get_account(storage))
    assert sum(f["fee"] for f in sells) == pytest.approx(5300. - matcher.sell_value(5300., next_day))
    assert paper.load_positions(storage)[SYM]["qty"] == 0


def test_stop_time_fallback_and_unfilled_remainder_are_explicit(setup):
    storage, _clock, _rule, repo, _event = setup
    entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)))
    advance(setup, datetime.combine(DAY, time(14, 35)))
    next_day = DAY + timedelta(days=1)
    repo.frame = bars(datetime.combine(next_day, time(10, 30)), n=1, close=9., volume=10.)
    advance(setup, datetime.combine(next_day, time(10, 30)))
    sell = next(o for o in paper.load_orders(storage) if o["side"] == "sell")
    assert sell["strategy_execution"]["exit_reason"] == "stop_time"
    assert [f for f in paper.load_fills(storage) if f["side"] == "sell"] == []
    advance(setup, datetime.combine(next_day, time(10, 45)))
    assert paper.get_order(storage, sell["id"])["status"] == "expired"
    assert "余股" in paper.get_order(storage, sell["id"])["reason"]
    assert paper.load_positions(storage)[SYM]["qty"] == 500


def test_file_restart_recovers_fill_appended_before_order_save(setup, monkeypatch):
    storage, _clock, _rule, repo, _event = setup
    order = entry(setup)
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start)
    original = paper.save_order
    armed = [True]

    def fail_after_append(data, value, *args, **kwargs):
        if value.get("filled_qty") and armed[0]:
            armed[0] = False
            raise RuntimeError("response lost")
        return original(data, value, *args, **kwargs)

    monkeypatch.setattr(paper, "save_order", fail_after_append)
    with pytest.raises(RuntimeError, match="response lost"):
        advance(setup, start)
    advance(setup, start)
    assert len(paper.load_fills(storage)) == 1
    assert paper.get_order(storage, order["id"])["filled_qty"] == 100
    assert paper.get_account(storage)["cash"] == pytest.approx(
        paper.replay_positions(storage)[1], abs=.01)


@pytest.mark.parametrize("close", [10.6, 9.4])
def test_rise_and_fall_use_original_previous_close(setup, close):
    storage, _clock, _rule, repo, _event = setup
    entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)), volume=10.)
    advance(setup, datetime.combine(DAY, time(14, 35)))
    nxt = DAY + timedelta(days=1)
    repo.frame = bars(datetime.combine(nxt, time(9, 31)), n=1, close=close, volume=10.)
    advance(setup, datetime.combine(nxt, time(9, 31)))
    sell = next(o for o in paper.load_orders(storage) if o["side"] == "sell")
    assert sell["strategy_execution"]["exit_reason"] == ("rise" if close > 10 else "fall")
    assert paper.load_fills(storage)[-1]["price"] == close


def test_missing_exit_minute_fails_without_inventing_a_fill(setup):
    storage, _clock, _rule, repo, _event = setup
    entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)))
    advance(setup, datetime.combine(DAY, time(14, 35)))
    nxt = DAY + timedelta(days=1)
    repo.frame = bars(datetime.combine(nxt, time(9, 31)), n=1, close=10.6)
    advance(setup, datetime.combine(nxt, time(9, 31)))
    repo.frame = pl.concat([repo.frame, bars(datetime.combine(nxt, time(9, 33)), n=1, close=10.6)])
    advance(setup, datetime.combine(nxt, time(9, 33)))
    sell = next(o for o in paper.load_orders(storage) if o["side"] == "sell")
    assert sell["status"] == "expired" and "缺失分钟" in sell["reason"]
    assert paper.load_positions(storage)[SYM]["qty"] == 400


def test_processed_buy_minute_revision_stops_future_execution(setup):
    storage, _clock, _rule, repo, _event = setup
    order = entry(setup)
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start)
    advance(setup, start)
    repo.frame = bars(start, high=10.3)
    advance(setup, start + timedelta(minutes=1))
    assert paper.get_order(storage, order["id"])["status"] == "expired"
    assert len(paper.load_fills(storage)) == 1


def test_account_freezes_costs_and_reserves_only_unspent_budget(setup):
    storage, _clock, _rule, repo, _event = setup
    entry(setup)
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start)
    advance(setup, start)
    cash = paper.get_account(storage)["cash"]
    reserved = paper.portfolio_budget(storage)["pending"][SYM]
    assert cash - reserved == pytest.approx(800_000., abs=.01)
    paper.update_settings(storage, min_commission=500., commission_pct=.001)
    advance(setup, start + timedelta(minutes=4))
    assert sum(f["fee"] for f in paper.load_fills(storage)) < 20.


def test_worker_recovery_after_window_does_not_invent_historical_buys(setup):
    storage, _clock, _rule, repo, _event = setup
    order = entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)))
    advance(setup, datetime.combine(DAY, time(14, 36)))
    assert paper.get_order(storage, order["id"])["status"] == "cancelled"
    assert paper.load_fills(storage) == []


@pytest.mark.parametrize("changes", [
    {"match_id": "other"}, {"side": "buy"}, {"candidate_priority": "pool"},
    {"cooldown_days": 1}, {"execution_mode": "unsupported"},
])
def test_s02_mode_rejects_incompatible_rules(setup, changes):
    storage, _clock, rule, _repo, _event = setup
    with pytest.raises(ValueError):
        paper_auto.update_auto_rule(storage, rule["id"], changes)
    assert paper_auto.load_auto_rules(storage)[0] == rule
