from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import polars as pl

from app.market_time import cn_today
from app.services import alert_store, preferences, quote_service
from app.services.quote_service import QuoteService
from app.strategy.engine import StrategyResult
from app.strategy.monitor import MonitorRuleEngine


def _strategy_rule(**overrides) -> dict:
    return {
        "id": "strategy_rule",
        "name": "策略监控",
        "type": "strategy",
        "asset_type": "stock",
        "scope": "all",
        "symbols": [],
        "strategy_id": "demo",
        "notify_events": ["pool_entry"],
        "cooldown_seconds": 0,
        **overrides,
    }


def _price_rule() -> dict:
    return {
        "id": "price_rule",
        "name": "价格监控",
        "type": "price",
        "asset_type": "stock",
        "scope": "all",
        "symbols": [],
        "conditions": [{"field": "close", "op": ">", "value": 0}],
        "logic": "and",
        "cooldown_seconds": 0,
    }


def _quotes() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["A"],
        "name": ["测试股票"],
        "close": [10.0],
        "raw_close": [10.0],
        "change_pct": [0.01],
    })


def _result(*symbols: str) -> StrategyResult:
    return StrategyResult(
        as_of=date(2026, 10, 19),
        strategy_id="demo",
        rows=[
            {"symbol": symbol, "name": symbol, "close": 10.0, "raw_close": 10.0}
            for symbol in symbols
        ],
        total=len(symbols),
    )


class _SequenceStrategyEngine:
    def __init__(self, results: list[StrategyResult]) -> None:
        self.results = list(results)
        self.run_count = 0
        self.strategy = SimpleNamespace(
            meta={"id": "demo", "name": "示例策略"},
            execution_backend="polars_expr",
            filter_history_fn=None,
        )

    def get(self, strategy_id: str):
        assert strategy_id == "demo"
        return self.strategy

    def run(self, strategy_id: str, context, **_kwargs):
        assert strategy_id == "demo"
        self.run_count += 1
        return self.results.pop(0)


def test_realtime_rules_do_not_execute_strategy_and_strategy_state_publishes_later() -> None:
    strategy_engine = _SequenceStrategyEngine([_result(), _result("A")])
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_rules([_strategy_rule(), _price_rule()])

    realtime = engine.evaluate_realtime_rules(_quotes(), asset_type="stock")
    assert [event["rule_id"] for event in realtime] == ["price_rule"]
    assert strategy_engine.run_count == 0

    first = engine.capture_strategy_group({"stock": _quotes()})
    assert first is not None
    first_published = engine.publish_strategy_group(engine.compute_strategy_group(first))
    assert engine.expose_strategy_group_results(first_published) is True
    assert first_published.discarded is False
    assert first_published.events == ()
    assert engine.latest_strategy_results()["demo"]["total"] == 0

    second = engine.capture_strategy_group({"stock": _quotes()})
    assert second is not None
    second_published = engine.publish_strategy_group(engine.compute_strategy_group(second))
    assert engine.expose_strategy_group_results(second_published) is True
    assert [(event["type"], event["symbol"]) for event in second_published.events] == [
        ("pool_entry", "A"),
    ]
    assert strategy_engine.run_count == 2


def test_strategy_publication_rejects_result_from_changed_rule_plan() -> None:
    strategy_engine = _SequenceStrategyEngine([_result(), _result("A")])
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_rules([_strategy_rule()])

    baseline = engine.capture_strategy_group({"stock": _quotes()})
    assert baseline is not None
    baseline_published = engine.publish_strategy_group(engine.compute_strategy_group(baseline))
    assert engine.expose_strategy_group_results(baseline_published) is True
    assert engine.latest_strategy_results()["demo"]["total"] == 0

    stale_plan = engine.capture_strategy_group({"stock": _quotes()})
    assert stale_plan is not None
    evaluation = engine.compute_strategy_group(stale_plan)
    engine.set_rules([_strategy_rule(scope="symbols", symbols=["B"])])

    published = engine.publish_strategy_group(evaluation)
    assert published.discarded is True
    assert published.discard_reason == "strategy plan changed"
    assert engine.latest_strategy_results()["demo"]["total"] == 0


def test_same_strategy_is_evaluated_once_for_multiple_rules_in_one_cycle() -> None:
    strategy_engine = _SequenceStrategyEngine([_result("A")])
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_rules([
        _strategy_rule(id="strategy_rule_a"),
        _strategy_rule(id="strategy_rule_b"),
    ])

    snapshot = engine.capture_strategy_group({"stock": _quotes()})
    assert snapshot is not None
    evaluation = engine.compute_strategy_group(snapshot)

    assert evaluation.strategy_run_count == 1
    assert strategy_engine.run_count == 1
    assert evaluation.results["demo"]["total"] == 1


def test_precomputed_strategy_result_is_filtered_per_rule_scope() -> None:
    strategy_engine = _SequenceStrategyEngine([_result(), _result("A", "B")])
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_rules([
        _strategy_rule(
            id="strategy_rule_a",
            scope="symbols",
            symbols=["A"],
        ),
        _strategy_rule(
            id="strategy_rule_b",
            scope="symbols",
            symbols=["B"],
        ),
    ])

    both_quotes = pl.concat([
        _quotes(),
        _quotes().with_columns(
            pl.lit("B").alias("symbol"),
            pl.lit("测试股票B").alias("name"),
        ),
    ])
    first = engine.capture_strategy_group({"stock": both_quotes})
    assert first is not None
    engine.publish_strategy_group(engine.compute_strategy_group(first))

    second = engine.capture_strategy_group({"stock": both_quotes})
    assert second is not None
    evaluation = engine.compute_strategy_group(second)

    assert strategy_engine.run_count == 2
    assert evaluation.results["demo"]["total"] == 2
    assert {
        (event["rule_id"], event["type"], event["symbol"])
        for event in evaluation.events
    } == {
        ("strategy_rule_a", "pool_entry", "A"),
        ("strategy_rule_b", "pool_entry", "B"),
    }
    assert {
        event["rule_id"]: event["strategy_priority"]
        for event in evaluation.execution_events
    } == {
        "strategy_rule_a": 0,
        "strategy_rule_b": 1,
    }


class _SharedPreparationStrategyEngine:
    def __init__(self, *, matrix: bool = False) -> None:
        backend = "matrix_native" if matrix else "polars_expr"
        self.strategies = {
            sid: SimpleNamespace(
                meta={"id": sid, "name": sid},
                execution_backend=backend,
                filter_history_fn=None if matrix else object(),
                lookback_days=5 if sid == "first" else 8,
            )
            for sid in ("first", "second")
        }
        self.run_calls: list[str] = []
        self.history_frames: list[pl.DataFrame] = []
        self.prepare_calls: list[list[str]] = []
        self.matrix = object()

    def get(self, strategy_id: str):
        return self.strategies[strategy_id]

    def required_history_bars(self, strategy_ids, **_kwargs):
        return max(self.strategies[sid].lookback_days for sid in strategy_ids)

    def prepare_realtime_matrix(self, context, strategy_ids, **_kwargs):
        self.prepare_calls.append(list(strategy_ids))
        assert context.history is not None
        return self.matrix

    def run(self, strategy_id: str, context, **_kwargs):
        self.run_calls.append(strategy_id)
        assert context.history is not None
        self.history_frames.append(context.history)
        if self.strategies[strategy_id].execution_backend == "matrix_native":
            assert context.market is self.matrix
        return StrategyResult(
            as_of=date(2026, 10, 19),
            strategy_id=strategy_id,
        )


class _PartiallyFailingStrategyEngine(_SharedPreparationStrategyEngine):
    def __init__(self) -> None:
        super().__init__()
        for strategy in self.strategies.values():
            strategy.filter_history_fn = None

    def required_history_bars(self, strategy_ids, **_kwargs):
        return 1

    def run(self, strategy_id: str, context, **_kwargs):
        self.run_calls.append(strategy_id)
        if strategy_id == "second":
            raise RuntimeError("injected strategy failure")
        return StrategyResult(
            as_of=date(2026, 10, 19),
            strategy_id=strategy_id,
            rows=[{"symbol": "A", "close": 10.0, "raw_close": 10.0}],
            total=1,
        )


def test_partial_strategy_cycle_blocks_entries_but_keeps_exits_for_execution() -> None:
    strategy_engine = _PartiallyFailingStrategyEngine()
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_rules([
        _strategy_rule(id="first_rule", strategy_id="first"),
        _strategy_rule(id="second_rule", strategy_id="second"),
    ])

    snapshot = engine.capture_strategy_group({"stock": _quotes()})
    assert snapshot is not None
    evaluation = engine.publish_strategy_group(engine.compute_strategy_group(snapshot))
    assert evaluation.partial is True
    assert evaluation.failed_strategy_ids == ("second",)
    assert set(evaluation.results) == {"first"}

    buy = {
        "source": "strategy",
        "strategy_id": "first",
        "strategy_priority": 0,
        "type": "buy_signal",
        "symbol": "A",
    }
    sell = {**buy, "type": "sell_signal"}
    evaluation = replace(
        evaluation,
        events=(buy, sell),
        execution_events=(buy, sell),
    )
    service = QuoteService()
    service._strategy_group_engine = engine
    service._strategy_group_consumed_sequence = 0
    service._strategy_group_coordinator = SimpleNamespace(
        latest_success=lambda: (
            SimpleNamespace(
                sequence=1,
                cycle_id="realtime-monitor:1",
                captured_at=time.time(),
            ),
            evaluation,
        ),
    )

    events, executions, updated = service._consume_strategy_group(engine)

    assert [event["type"] for event in events] == ["buy_signal", "sell_signal"]
    assert [event["type"] for event in executions] == ["sell_signal"]
    assert updated is True
    assert {
        (event["strategy_cycle_id"], event["strategy_cycle_state"])
        for event in events + executions
    } == {("realtime-monitor:1", "partial")}


def test_strategy_group_loads_max_history_once_and_shares_the_same_panel() -> None:
    strategy_engine = _SharedPreparationStrategyEngine()
    history_calls: list[tuple[date, int]] = []
    history = _quotes().with_columns(pl.lit(date(2026, 10, 18)).alias("date"))
    current = _quotes().with_columns(pl.lit(date(2026, 10, 19)).alias("date"))
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_history_loader(
        lambda target, lookback: history_calls.append((target, lookback)) or history,
    )
    engine.set_rules([
        _strategy_rule(id="first_rule", strategy_id="first"),
        _strategy_rule(id="second_rule", strategy_id="second"),
    ])

    snapshot = engine.capture_strategy_group({"stock": current})
    assert snapshot is not None
    evaluation = engine.compute_strategy_group(snapshot)

    assert history_calls == [(cn_today(), 8)]
    assert evaluation.history_load_count == 1
    assert evaluation.strategy_run_count == 2
    assert strategy_engine.run_calls == ["first", "second"]
    assert strategy_engine.history_frames[0] is strategy_engine.history_frames[1]


def test_strategy_group_prepares_one_matrix_for_unique_strategy_ids() -> None:
    strategy_engine = _SharedPreparationStrategyEngine(matrix=True)
    history = _quotes().with_columns(pl.lit(date(2026, 10, 18)).alias("date"))
    current = _quotes().with_columns(pl.lit(date(2026, 10, 19)).alias("date"))
    history_calls = 0

    def load_history(_target, _lookback):
        nonlocal history_calls
        history_calls += 1
        return history

    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_history_loader(load_history)
    engine.set_rules([
        _strategy_rule(id="first_a", strategy_id="first"),
        _strategy_rule(id="first_b", strategy_id="first"),
        _strategy_rule(id="second", strategy_id="second"),
    ])

    snapshot = engine.capture_strategy_group({"stock": current})
    assert snapshot is not None
    evaluation = engine.compute_strategy_group(snapshot)

    assert history_calls == 1
    assert evaluation.history_load_count == 1
    assert evaluation.matrix_prepare_count == 1
    assert evaluation.strategy_run_count == 2
    assert strategy_engine.prepare_calls == [["first", "second"]]
    assert strategy_engine.run_calls == ["first", "second"]


def test_stale_consumption_drops_entries_but_keeps_exits_and_previous_results() -> None:
    strategy_engine = _SequenceStrategyEngine([_result(), _result("A"), _result()])
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_rules([_strategy_rule(notify_events=["pool_entry", "pool_exit"])])

    baseline = engine.capture_strategy_group({"stock": _quotes()})
    assert baseline is not None
    baseline_result = engine.publish_strategy_group(engine.compute_strategy_group(baseline))
    assert engine.expose_strategy_group_results(baseline_result) is True
    assert engine.consume_strategy_result_updates() is True

    entry = engine.capture_strategy_group({"stock": _quotes()})
    assert entry is not None
    entry_result = engine.publish_strategy_group(engine.compute_strategy_group(entry))
    assert [event["type"] for event in entry_result.events] == ["pool_entry"]

    service = QuoteService()
    service._strategy_group_engine = engine
    service._strategy_group_consumed_sequence = 0
    service._strategy_group_coordinator = SimpleNamespace(
        latest_success=lambda: (
            SimpleNamespace(sequence=1, cycle_id="stale-entry", captured_at=time.time() - 20),
            entry_result,
        ),
    )
    assert service._consume_strategy_group(engine) == ([], [], False)
    assert engine.latest_strategy_results()["demo"]["total"] == 0

    exit_snapshot = engine.capture_strategy_group({"stock": _quotes()})
    assert exit_snapshot is not None
    exit_result = engine.publish_strategy_group(engine.compute_strategy_group(exit_snapshot))
    service._strategy_group_coordinator = SimpleNamespace(
        latest_success=lambda: (
            SimpleNamespace(sequence=2, cycle_id="stale-exit", captured_at=time.time() - 20),
            exit_result,
        ),
    )
    events, executions, updated = service._consume_strategy_group(engine)
    assert [(event["type"], event["symbol"]) for event in events] == [("pool_exit", "A")]
    assert [(event["type"], event["symbol"]) for event in executions] == [("pool_exit", "A")]
    assert updated is False
    assert engine.latest_strategy_results()["demo"]["total"] == 0


class _BlockingStrategyEngine(_SequenceStrategyEngine):
    def __init__(self) -> None:
        super().__init__([])
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, strategy_id: str, context, **_kwargs):
        assert strategy_id == "demo"
        self.run_count += 1
        self.started.set()
        assert self.release.wait(10)
        return _result()


def test_quote_service_keeps_realtime_rules_per_snapshot_and_coalesces_strategy_work(
    monkeypatch,
    tmp_path,
) -> None:
    strategy_engine = _BlockingStrategyEngine()
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(strategy_engine)
    engine.set_rules([_strategy_rule(), _price_rule()])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest_asset=lambda *_args, **_kwargs: (pl.DataFrame(), None),
    )
    service = QuoteService()
    service.set_app_state(SimpleNamespace(monitor_engine=engine, repo=repo))
    service._repo = repo
    service.get_enriched_today = lambda: (_quotes(), cn_today())
    subscriber = service.subscribe()

    monkeypatch.setattr(alert_store, "append_many", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(preferences, "get_system_notify_enabled", lambda: False)
    monkeypatch.setattr(quote_service, "_monitor_name_map", lambda _repo: {})

    try:
        with patch.object(QuoteService, "_is_continuous_trading", return_value=True):
            for _ in range(3):
                service._evaluate_monitors(pl.DataFrame(), None)
                message = subscriber.pop()
                assert [event["rule_id"] for event in message["alerts"]] == ["price_rule"]
                assert message["strategy_results_updated"] is False

            assert strategy_engine.started.wait(1)
            status = service._strategy_group_coordinator.status()
            assert status["counts"]["submitted"] == 3
            assert status["counts"]["superseded"] == 1

            strategy_engine.release.set()
            assert service._strategy_group_coordinator.wait_idle(timeout=2)
            assert strategy_engine.run_count == 2
            service._evaluate_monitors(pl.DataFrame(), None)
            assert subscriber.pop()["strategy_results_updated"] is True
    finally:
        strategy_engine.release.set()
        service.stop()
