"""持仓日期、因果振幅和截止时钟的早盘清仓契约。"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api.signals import SignalModel
from app.strategy import custom_signals

DAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 7, 10)


def definition(**params):
    return {"id": "morning_liquidation", "name": "早盘清仓", "kind": "exit",
            "timeframe": "intraday", "template": "morning_liquidation",
            "conditions": [], "enabled": True, "params": {"time": "10:30:00", "amplitude_pct": 6, **params}}


def position(**patch):
    return {"symbol": "a", "entry_date": "2026-09-04", "qty": 100,
            "available_qty": 100, "account_id": "one", "cycle_id": "lot1", **patch}


def bars(high=10.6, low=10):
    return pl.DataFrame({"symbol": ["a"], "datetime": [NOW],
                         "high": [high], "low": [low], "close": [low]})


def evaluate(frame=None, sig=None, **kwargs):
    from app.strategy.morning_liquidation import evaluate

    return evaluate(bars() if frame is None else frame, sig or definition(),
                    day=DAY, now=kwargs.pop("now", NOW),
                    positions=kwargs.pop("positions", [position()]),
                    prev_close=kwargs.pop("prev_close", {"a": 10}), **kwargs)


def test_template_roundtrip_and_no_generic_compilation(tmp_path):
    model = SignalModel(**definition()).model_dump()
    custom_signals.validate(model)
    custom_signals.save_one(tmp_path, model)
    assert custom_signals.load_all(tmp_path) == [model]
    assert custom_signals.build_expressions([model]) == {}
    assert custom_signals.build_intraday_expressions([model]) == {}


@pytest.mark.parametrize("params", [
    {"time": "09:29:59"}, {"time": "11:30:01"}, {"time": "10:3:00"},
    {"amplitude_pct": -1}, {"amplitude_pct": 101}, {"amplitude_pct": float("nan")},
    {"amplitude_pct": True}, {"unexpected": 1},
])
def test_invalid_parameters(params):
    with pytest.raises(ValueError):
        custom_signals.validate(definition(**params))


def test_inclusive_threshold_uses_range_not_profit_and_retains_holding_identity():
    hit = evaluate().hits[0]
    assert hit["reason"] == "amplitude"
    assert hit["amplitude_pct"] == pytest.approx(6)
    assert hit["trigger_time"] == NOW.isoformat()
    assert hit["source"] == "completed_minute"
    assert hit["account_id"] == "one" and hit["cycle_id"] == "lot1"
    assert not evaluate(frame=bars(10.599)).hits
    assert not evaluate(frame=bars(10.6, 10.6)).hits  # 涨幅6%但振幅0%
    assert evaluate(sig=definition(amplitude_pct=0), frame=bars(10, 10)).hits


def test_deadline_is_unconditional_and_supports_seconds_without_fake_price():
    assert not evaluate(frame=pl.DataFrame(), now=NOW.replace(minute=29, second=59)).hits
    hit = evaluate(frame=pl.DataFrame(), prev_close={}, now=NOW.replace(minute=30)).hits[0]
    assert hit["reason"] == "deadline" and hit["amplitude_pct"] is None
    assert "price" not in hit
    assert hit["trigger_time"].endswith("10:30:00")
    sig = definition(time="10:15:32")
    assert not evaluate(frame=bars(10), sig=sig, now=NOW.replace(minute=15, second=31)).hits
    assert evaluate(frame=bars(10), sig=sig, now=NOW.replace(minute=15, second=32)).hits


def test_next_trading_day_skips_weekend_and_national_day_holidays():
    assert evaluate().hits
    from app.strategy.morning_liquidation import evaluate as evaluate_morning

    day = date(2026, 10, 8)
    result = evaluate_morning(pl.DataFrame(), definition(), day=day, now=datetime(2026, 10, 8, 10, 30),
                      positions=[position(entry_date="2026-09-30")], prev_close={})
    assert result.hits


@pytest.mark.parametrize("patch", [
    {"entry_date": "2026-09-07"}, {"available_qty": 0}, {"qty": 0},
    {"entry_date": "2026-09-03"},
])
def test_no_same_day_unavailable_or_other_day_exit(patch):
    assert not evaluate(positions=[position(**patch)], now=NOW.replace(minute=30)).hits


@pytest.mark.parametrize("entry", [None, "wrong", "2026-09-05", "2027-01-04"])
def test_missing_invalid_or_unknown_entry_date_has_diagnostic(entry):
    result = evaluate(positions=[position(entry_date=entry)])
    assert not result.hits and result.diagnostics
    assert not evaluate(positions=None).hits


def test_no_future_bar_or_stale_quote_and_restart_keeps_first_minute_trigger():
    future = bars(12).with_columns((pl.col("datetime")+pl.duration(minutes=10)).alias("datetime"))
    assert not evaluate(frame=future).hits
    frame = pl.concat([bars(), future], how="vertical_relaxed")
    assert evaluate(frame=frame, now=NOW+timedelta(minutes=20)).hits[0]["trigger_time"] == NOW.isoformat()
    quote = pl.DataFrame({"symbol": ["a"], "high": [10.8], "low": [10],
                          "prev_close": [10], "quote_ts": [NOW.timestamp()]})
    # Naive fixture timestamps must be interpreted explicitly in Beijing.
    from app.market_time import CN_TZ

    quote = quote.with_columns(pl.lit(NOW.replace(tzinfo=CN_TZ).timestamp()).alias("quote_ts"))
    hit = evaluate(frame=bars(10), quotes=quote, now=NOW+timedelta(seconds=4)).hits[0]
    assert hit["source"] == "live_quote"
    assert not evaluate(frame=bars(10), quotes=quote, now=NOW+timedelta(seconds=16)).hits
    assert not evaluate(frame=bars(10), quotes=quote, now=NOW-timedelta(seconds=3)).hits
    aware = bars().with_columns(pl.col("datetime").dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone("UTC"))
    assert evaluate(frame=aware).hits


def test_bad_range_or_reference_does_not_prove_threshold():
    assert not evaluate(frame=bars(float("nan"))).hits
    assert not evaluate(frame=bars(10, 11)).hits
    assert not evaluate(prev_close={"a": 0}).hits
    assert evaluate(frame=bars(float("nan")), prev_close={}, now=NOW.replace(minute=30)).hits


def sample_engine(tmp_path):
    from app.strategy.engine import StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock"], "timeframes": ["1m"], "execution_backend": "minute_filter"}
BASIC_FILTER = {"enabled": False}
def filter_minute_history(df, params):
    return df.filter(pl.col("symbol") == "b").group_by("symbol").agg(pl.col("datetime").max().alias("last_datetime"))
''')
    engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, definition())
    return engine


def test_engine_exits_holdings_independently_of_candidate_pool_and_empty_minutes(tmp_path):
    from app.strategy.engine import StrategyDataContext

    engine = sample_engine(tmp_path)
    context = StrategyDataContext("stock", "1m", DAY, history=bars(), positions=[position()], observed_at=NOW,
                                  pool_history=pl.DataFrame({"symbol": ["a"], "date": [date(2026, 9, 4)], "close": [10.]}))
    result = engine.run("sample", context, pool=["b"], overrides={"exit_signals": ["csgi_morning_liquidation"]})
    assert not result.rows
    assert result.exit_signal_hits[0]["symbol"] == "a"
    assert result.exit_signal_hits[0]["signals"] == ["csgi_morning_liquidation"]
    context.history = pl.DataFrame()
    context.observed_at = NOW.replace(minute=30)
    assert engine.run("sample", context, overrides={"exit_signals": ["csgi_morning_liquidation"]}).exit_signal_hits
    context.positions = None
    failed_exit = engine.run("sample", context, overrides={"exit_signals": ["csgi_morning_liquidation"]})
    assert "持仓" in failed_exit.exit_filter_error
    assert not failed_exit.exit_signal_hits


@pytest.mark.parametrize("entry_at,evaluated_at,allowed", [
    (NOW, None, False),
    (NOW.replace(hour=14, minute=30), None, True),
    (NOW, NOW + timedelta(seconds=90), True),
])
def test_engine_morning_exit_only_excludes_buy_at_the_same_signal_time(
    tmp_path, entry_at, evaluated_at, allowed,
):
    from app.strategy.engine import StrategyDataContext

    engine = sample_engine(tmp_path)
    path = tmp_path / "strategies" / "custom" / "sample.py"
    path.write_text(path.read_text().replace('pl.col("symbol") == "b"', 'pl.col("symbol") == "a"'))
    engine.reload()
    history = bars() if entry_at == NOW else pl.concat([
        bars(), bars().with_columns(pl.lit(entry_at).alias("datetime")),
    ])
    if evaluated_at is not None:
        history = history.with_columns(pl.lit(evaluated_at).alias("entry_evaluated_at"))
    context = StrategyDataContext(
        "stock", "1m", DAY, history=history, positions=[position()],
        observed_at=NOW.replace(hour=15),
        pool_history=pl.DataFrame({
            "symbol": ["a"], "date": [date(2026, 9, 4)], "close": [10.],
        }),
    )
    result = engine.run("sample", context, overrides={"exit_signals": ["csgi_morning_liquidation"]})
    assert [hit["symbol"] for hit in result.exit_signal_hits] == ["a"]
    assert [row["symbol"] for row in result.rows] == (["a"] if allowed else [])
    assert [hit["symbol"] for hit in result.entry_signal_hits] == (["a"] if allowed else [])


def test_replay_requires_explicit_entry_date_and_returns_trigger_reason(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router

    custom_signals.save_one(tmp_path, definition())

    class Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        def get_daily_batch(self, *args, **kwargs):
            return pl.DataFrame({"symbol": ["a"], "date": [date(2026, 9, 4)],
                                 "close": [99.], "raw_close": [10.]})

        def get_minute_batch(self, *args, **kwargs):
            return bars()

    app = FastAPI()
    app.state.repo = Repo()
    app.include_router(router)
    client = TestClient(app)
    payload = {"signal_id": "morning_liquidation", "start_date": str(DAY), "end_date": str(DAY),
               "symbols": ["a"], "positions": [position()]}
    response = client.post("/api/custom-signals/intraday/replay", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["triggers"][0]["reason"] == "amplitude"
    assert response.json()["triggers"][0]["time"] == "10:00:00"
    assert client.post("/api/custom-signals/intraday/replay", json={**payload, "positions": []}).status_code == 400


def test_monitor_context_loads_owned_positions_and_keeps_seconds_without_minutes(tmp_path, monkeypatch):
    from app.strategy import overnight_runtime

    engine = sample_engine(tmp_path)
    engine._override_loader = lambda sid: {"exit_signals": ["csgi_morning_liquidation"]}
    engine._position_loader = lambda sid, day: [position()]

    class Service:
        def build_strategy_context(self, *args, **kwargs):
            raise ValueError("缺少分钟数据")

        def load(self):
            pass

    monkeypatch.setattr(overnight_runtime, "cn_now", lambda: NOW.replace(minute=30, second=7))
    context = overnight_runtime.monitor_context(Service().load, engine, "sample", DAY, pl.DataFrame(), tmp_path)
    assert context.positions == [position()]
    assert context.observed_at.second == 7
    assert context.input_errors == ["缺少分钟数据"]
    result = engine.run("sample", context, overrides={"exit_signals": ["csgi_morning_liquidation"]})
    assert result.entry_filter_error == "缺少分钟数据"
    assert result.rows == [] and result.entry_signal_hits == []
    assert [hit["symbol"] for hit in result.exit_signal_hits] == ["a"]


def test_legacy_monitor_reads_saved_exit_override_without_engine_loader(tmp_path, monkeypatch):
    from app.strategy import config, overnight_runtime

    engine = sample_engine(tmp_path)
    config.save_override(tmp_path, "sample", {"exit_signals": ["csgi_morning_liquidation"]})
    engine._position_loader = lambda sid, day: [position()]

    class Service:
        def build_strategy_context(self, *args, **kwargs):
            raise ValueError("缺少分钟数据")

        def load(self):
            pass

    monkeypatch.setattr(overnight_runtime, "cn_now", lambda: NOW.replace(minute=30))
    context = overnight_runtime.monitor_context(Service().load, engine, "sample", DAY, pl.DataFrame(), tmp_path)
    assert context.positions == [position()]


@pytest.mark.parametrize("execution", ["daily", "special"])
def test_backtest_explicitly_rejects_unsupported_holding_exit(tmp_path, execution):
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    engine = sample_engine(tmp_path)
    strategy = engine.get("sample")
    if execution == "daily":
        strategy.execution_backend = "polars_expr"
        strategy.meta["timeframes"] = ["1d"]
    else:
        strategy.meta["minute_execution"] = "overnight_capacity_v1"
    service = StrategyBacktestService(SimpleNamespace(repo=object()), engine)
    result = service.run(StrategyBacktestConfig(
        strategy_id="sample", start=DAY, end=DAY, symbols=["a"],
        overrides={"exit_signals": ["csgi_morning_liquidation"]},
    ))
    assert "早盘清仓" in result.error and "回测" in result.error


def test_raw_signal_monitor_rejected_without_holding_dates(tmp_path):
    from fastapi import HTTPException

    from app.api.monitor_rules import RuleModel, save_rule

    custom_signals.save_one(tmp_path, definition())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path)))))
    with pytest.raises(HTTPException) as exc:
        save_rule(RuleModel(id="raw", name="test", type="signal", scope="symbols", symbols=["a"],
                            conditions=[{"field": "csgi_morning_liquidation", "op": "==", "value": 1}]), request)
    assert exc.value.status_code == 400 and "持仓" in exc.value.detail


def test_snapshot_keeps_each_purchase_date_and_only_owned_strategy(tmp_path):
    from app.strategy import paper
    from app.strategy.morning_liquidation import holding_snapshot

    paper.create_account(tmp_path, 10000, account_id="one", name="test")
    for index, (entry, strategy) in enumerate([("2026-09-03", "sample"), ("2026-09-04", "sample"),
                                               ("2026-09-04", "other")]):
        paper._append_fill(tmp_path, {"symbol": "a" if index < 2 else "b", "side": "buy",
            "qty": 100, "price": 10., "fee": 0., "date": entry, "order_id": str(index),
            "entry_policy": {"strategy_id": strategy}}, "one")
    snapshots = holding_snapshot(tmp_path, "sample", DAY)
    assert [row["entry_date"] for row in snapshots] == ["2026-09-03", "2026-09-04"]
    result = evaluate(positions=snapshots)
    assert len(result.hits) == 1 and result.hits[0]["available_qty"] == 100


def test_cached_monitor_does_not_drop_owned_holding_exit_outside_candidate_rows(tmp_path):
    from app.strategy.engine import StrategyResult
    from app.strategy.monitor import _PrecomputedMonitorStrategyEngine

    source = sample_engine(tmp_path)
    hit = {"symbol": "a", "signals": ["csgi_morning_liquidation"], "holding_exit": True}
    cached = _PrecomputedMonitorStrategyEngine(source, {("stock", "sample"): StrategyResult(
        strategy_id="sample", as_of=DAY, exit_signal_hits=[hit])}, {})
    assert cached.result_for("stock", "sample", {"b"}).exit_signal_hits == [hit]


def suspension_events(*, late=False):
    return pl.DataFrame({
        "symbol": ["a", "a"],
        "effective_date": [DAY, date(2026, 9, 9)],
        "announced_on": [DAY if late else date(2026, 9, 4), date(2026, 9, 9)],
        "announced_at": [datetime(2026, 9, 7, 19) if late else datetime(2026, 9, 4, 19),
                         datetime(2026, 9, 9, 8, 21)],
        "is_suspended": [True, False],
        "source_url": ["https://example.test/halt", "https://example.test/resume"],
    })


def test_confirmed_suspension_suppresses_deadline_and_resume_exits_immediately():
    from app.strategy.morning_liquidation import evaluate as evaluate_morning

    events = suspension_events()
    halted = evaluate(frame=pl.DataFrame(), prev_close={}, now=NOW.replace(minute=30),
                      suspension_history=events)
    assert not halted.hits
    assert any("停牌" in message for message in halted.diagnostics)
    resumed = evaluate_morning(
        pl.DataFrame(), definition(), day=date(2026, 9, 9), now=datetime(2026, 9, 9, 9, 30),
        positions=[position()], prev_close={}, suspension_history=events,
    )
    hit, = resumed.hits
    assert hit["reason"] == "resume" and hit["source"] == "suspension_evidence"
    assert hit["trigger_time"] == "2026-09-09T09:30:00"
    assert hit["available_qty"] == 100 and hit["cycle_id"] == "lot1"
    assert not hit["deadline_elapsed"]


def test_late_or_unknown_halt_cannot_justify_overdue_exit():
    from app.strategy.morning_liquidation import evaluate as evaluate_morning

    assert evaluate(frame=pl.DataFrame(), now=NOW.replace(minute=30),
                    suspension_history=suspension_events(late=True)).hits[0]["reason"] == "deadline"
    for events in (suspension_events(late=True), pl.DataFrame()):
        result = evaluate_morning(
            pl.DataFrame(), definition(), day=date(2026, 9, 9), now=datetime(2026, 9, 9, 10, 30),
            positions=[position()], prev_close={}, suspension_history=events,
        )
        assert not result.hits


def test_engine_resume_exit_keeps_evidence_outside_candidate_universe(tmp_path):
    from app.strategy.engine import StrategyDataContext

    engine = sample_engine(tmp_path)
    context = StrategyDataContext(
        "stock", "1m", date(2026, 9, 9), history=pl.DataFrame(), positions=[position()],
        observed_at=datetime(2026, 9, 9, 9, 31), suspension_history=suspension_events(),
        pool_universe=pl.DataFrame({"symbol": ["b"]}),
    )
    hit, = engine.run("sample", context, pool=["b"],
                     overrides={"exit_signals": ["csgi_morning_liquidation"]}).exit_signal_hits
    assert hit["symbol"] == "a" and hit["reason"] == "resume"


def test_monitor_missing_minutes_retains_repository_halt_evidence(tmp_path, monkeypatch):
    from app.strategy import overnight_runtime

    engine = sample_engine(tmp_path)
    engine._override_loader = lambda sid: {"exit_signals": ["csgi_morning_liquidation"]}
    engine._position_loader = lambda sid, day: [position()]

    class Service:
        repo = SimpleNamespace(get_suspension_history=suspension_events)

        def build_strategy_context(self, *args, **kwargs):
            raise ValueError("缺少分钟数据")

        def load(self):
            pass

    monkeypatch.setattr(overnight_runtime, "cn_now", lambda: NOW.replace(minute=30, second=7))
    context = overnight_runtime.monitor_context(Service().load, engine, "sample", DAY, pl.DataFrame(), tmp_path)
    assert context.input_errors == ["缺少分钟数据"]
    result = engine.run("sample", context, overrides={"exit_signals": ["csgi_morning_liquidation"]})
    assert result.entry_filter_error == "缺少分钟数据"
    assert result.rows == [] and result.entry_signal_hits == []
    assert result.exit_signal_hits == []


def test_signal_replay_uses_resume_evidence_without_simulating_fills():
    from app.services.morning_liquidation_replay import replay

    class Repo:
        def get_suspension_history(self):
            return suspension_events()

        def get_daily_batch(self, *args, **kwargs):
            return pl.DataFrame({"symbol": ["a"], "date": [date(2026, 9, 4)], "close": [10.], "raw_close": [10.]})

        def get_minute_batch(self, *args, **kwargs):
            return pl.DataFrame()

    result = replay(Repo(), definition(), start=DAY, end=date(2026, 9, 9), symbols=["a"], positions=[position()])
    hit, = result["triggers"]
    assert hit["reason"] == "resume" and hit["date"] == "2026-09-09"
    assert result["execution"] == "signal_only"


def test_resume_signal_cannot_precede_same_day_publication_time():
    from app.strategy.morning_liquidation import evaluate as evaluate_morning

    events = suspension_events().with_columns(
        pl.when(~pl.col("is_suspended")).then(datetime(2026, 9, 9, 11, 10))
        .otherwise(pl.col("announced_at")).alias("announced_at"),
    )
    day = date(2026, 9, 9)
    assert not evaluate_morning(
        pl.DataFrame(), definition(), day=day, now=datetime(2026, 9, 9, 11),
        positions=[position()], prev_close={}, suspension_history=events,
    ).hits
    hit, = evaluate_morning(
        pl.DataFrame(), definition(), day=day, now=datetime(2026, 9, 9, 15),
        positions=[position()], prev_close={}, suspension_history=events,
    ).hits
    assert hit["trigger_time"] == "2026-09-09T11:10:00"
