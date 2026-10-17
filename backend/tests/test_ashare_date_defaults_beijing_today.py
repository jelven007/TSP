"""A股业务日期缺省必须使用北京时间, 服务器时区不能成为隐式输入。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.api import factors as factors_api
from app.api import kline as kline_api
from app.api import regime as regime_api
from app.services import market_mainline, regime_builder, repair_daily

BJ = date(2026, 3, 2)
LOCAL = BJ - timedelta(days=1)


class _LocalDate(date):
    @classmethod
    def today(cls):
        return LOCAL


@pytest.fixture(autouse=True)
def distinct_clocks(monkeypatch):
    for module in (
        backtest_api, factors_api, regime_api, market_mainline, regime_builder, repair_daily,
    ):
        monkeypatch.setattr(module, "cn_today", lambda: BJ, raising=False)
        monkeypatch.setattr(module, "date", _LocalDate, raising=False)


@dataclass
class _DummyResult:
    status: str = "ok"


@pytest.mark.parametrize("explicit_end", [None, date(2026, 2, 27)])
def test_backtest_run_defaults_to_beijing_today(monkeypatch, explicit_end) -> None:
    captured_cfg = []

    class DummyService:
        def __init__(self, repo):
            pass

        def run(self, cfg):
            captured_cfg.append(cfg)
            return _DummyResult()

    monkeypatch.setattr(backtest_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(backtest_api, "BacktestService", DummyService)

    request_data = backtest_api.BacktestRequest(symbols=["600000.SH"], end=explicit_end)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    backtest_api.run(request_data, request)
    assert captured_cfg
    expected_end = explicit_end or BJ
    assert captured_cfg[0].end == expected_end
    assert captured_cfg[0].start == expected_end - timedelta(days=365 * 3)


@pytest.mark.parametrize("batch", [False, True])
def test_backtest_factor_run_defaults_to_beijing_today(monkeypatch, batch) -> None:
    captured_cfg = []

    class DummyFactorService:
        def __init__(self, engine):
            pass

        def run(self, cfg):
            captured_cfg.append(cfg)
            return _DummyResult()

        run_batch = run

    monkeypatch.setattr(backtest_api, "cn_today", lambda: BJ)
    monkeypatch.setattr("app.backtest.factor.FactorBacktestService", DummyFactorService)
    monkeypatch.setattr(backtest_api, "_get_engine", lambda request: MagicMock())
    monkeypatch.setattr(
        "app.factors.registry.factor_columns_view",
        lambda: [{"id": "mom_20d"}],
    )

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))
    if batch:
        request_data = backtest_api.FactorBatchRequest(factor_names=["mom_20d"])
        backtest_api.factor_batch(request_data, request)
    else:
        request_data = backtest_api.FactorBacktestRequest(factor_name="mom_20d")
        backtest_api.factor_run(request_data, request)
    assert captured_cfg
    assert captured_cfg[0].end == BJ


@pytest.mark.parametrize("save_check", [False, True])
def test_factors_trial_uses_beijing_today(monkeypatch, save_check) -> None:
    captured_args = []

    class DummyEngine:
        def load_panel(self, symbols, start, end, columns, asset_type):
            captured_args.append((start, end))
            return pl.DataFrame({
                **{column: [1.0, 2.0] for column in columns},
                "symbol": ["600000.SH", "600000.SH"],
                "date": [LOCAL, BJ],
            })

    monkeypatch.setattr(factors_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(
        factors_api,
        "compile_formula",
        lambda formula: SimpleNamespace(
            ok=True,
            warmup_bars=5,
            dependencies=frozenset(),
            referenced_factors=frozenset(),
            frame_transform=lambda panel: panel.with_columns(
                pl.col("close").alias(factors_api.FACTOR_COLUMN),
            ),
        ),
    )
    monkeypatch.setattr("app.api.backtest._get_engine", lambda request: DummyEngine())
    monkeypatch.setattr(
        "app.backtest.factor.FactorBacktestService._compute_missing_factors",
        lambda panel, to_compute: panel,
    )
    request_data = factors_api.FormulaTrialRequest(formula="close / open", days=20)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    if save_check:
        factors_api._trial_nonempty(request, request_data.formula)
    else:
        assert factors_api.trial_formula(request_data, request)["ok"] is True
    assert captured_args
    start, end = captured_args[0]
    expected_days = int((5 + (40 if save_check else 20)) * 1.6) + 15
    assert start == BJ - timedelta(days=expected_days)
    assert end == BJ


def test_regime_recompute_defaults_to_beijing_today(monkeypatch) -> None:
    captured_args = []

    def fake_run_batch(repo, start, end):
        captured_args.append((start, end))
        return pl.DataFrame()

    monkeypatch.setattr(regime_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(regime_api, "_data_dir", lambda request: MagicMock())
    monkeypatch.setattr(
        regime_builder,
        "earliest_enriched_date",
        lambda repo: date(2026, 1, 1),
    )
    monkeypatch.setattr(regime_builder, "run_regime_batch", fake_run_batch)
    monkeypatch.setattr(regime_builder, "refresh_phase_labels", lambda data_dir: 0)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))
    regime_api.regime_recompute(request, start=None, end=None)

    assert captured_args
    _start, end = captured_args[0]
    assert end == BJ


def test_repair_daily_validates_against_beijing_today(monkeypatch) -> None:
    monkeypatch.setattr(repair_daily, "cn_today", lambda: BJ)

    called = []

    def fake_run_now(*args, **kwargs):
        called.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("app.jobs.daily_pipeline.run_now", fake_run_now)

    result = repair_daily.run_repair_daily(MagicMock(), MagicMock(), start_date=BJ)
    assert result.get("ok") is True
    assert called

    future = repair_daily.run_repair_daily(
        MagicMock(),
        MagicMock(),
        start_date=BJ + timedelta(days=1),
    )
    assert future.get("error") == "起始日期不能晚于今天"


def test_market_mainline_incremental_uses_beijing_today(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(market_mainline, "cn_today", lambda: BJ)
    monkeypatch.setattr(
        market_mainline,
        "load_mainline_history",
        lambda data_dir, kind: pl.DataFrame(),
    )

    captured_range = []

    def fake_compute(repo, data_dir, start, end, kind="concept"):
        captured_range.append((start, end))
        return pl.DataFrame()

    monkeypatch.setattr(market_mainline, "compute_mainline_range", fake_compute)
    monkeypatch.setattr(
        "app.services.regime_builder.enriched_date_set",
        lambda repo: {date(2026, 3, 1), BJ, date(2026, 3, 3)},
    )

    market_mainline.compute_mainline_incremental(MagicMock(), tmp_path)

    assert captured_range
    start, end = captured_range[0]
    assert start == date(2026, 3, 1)
    assert end == BJ


@pytest.mark.parametrize("explicit_end", [None, date(2026, 2, 27)])
def test_strategy_run_uses_beijing_default_and_keeps_explicit_date(
    monkeypatch, tmp_path, explicit_end,
):
    captured = []
    monkeypatch.setattr(
        backtest_api, "_guard_minute_strategy_backtest", lambda *args: None,
    )
    monkeypatch.setattr("app.multitenancy.research.jobs_for", lambda request: None)

    def make_task(kind, data_dir, cfg):
        captured.append(cfg)
        return {}

    monkeypatch.setattr("app.backtest.worker.make_worker_task", make_task)
    monkeypatch.setattr("app.backtest.worker.run_worker_task", lambda task: {"ok": True})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))
    result = backtest_api.strategy_run(
        backtest_api.StrategyBacktestRequest(strategy_id="ma_cross", end=explicit_end),
        request,
    )
    assert result["ok"] is True
    assert captured[0].end == (explicit_end or BJ)


@pytest.mark.parametrize("endpoint", ["strategy", "optimize", "walkforward"])
@pytest.mark.parametrize("explicit_end", [None, "2026-02-27"])
def test_streams_resolve_beijing_default_before_range_guard(
    monkeypatch, endpoint, explicit_end,
):
    captured = []
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.repo = MagicMock()
    monkeypatch.setattr(
        backtest_api, "_guard_minute_strategy_backtest", lambda *args: None,
    )

    def record_end():
        captured.append(BJ)
        return BJ

    monkeypatch.setattr(backtest_api, "cn_today", record_end)
    monkeypatch.setattr(backtest_api.settings, "backtest_range_guard", True)
    params = {"strategy_id": "ma_cross", "start": "2020-01-01"}
    if explicit_end:
        params["end"] = explicit_end
    if endpoint != "strategy":
        params["param_grid"] = '{"n": [5]}'
    if endpoint == "walkforward":
        params.update({"train_days": 400, "test_days": 400})
    with TestClient(app) as client:
        response = client.get(f"/api/backtest/{endpoint}/stream", params=params)
    assert response.status_code == 200, response.text
    assert "event: error" in response.text
    assert captured == ([] if explicit_end else [BJ])


def test_regime_incremental_excludes_dates_after_beijing_today(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(regime_builder, "load_regime_history", lambda path: pl.DataFrame())
    monkeypatch.setattr(regime_builder, "detect_stale_dates", lambda *args: [])
    monkeypatch.setattr(
        regime_builder, "enriched_date_set", lambda repo: {LOCAL, BJ, BJ + timedelta(days=1)},
    )

    def run_batch(repo, start, end):
        captured.append((start, end))
        return pl.DataFrame()

    monkeypatch.setattr(regime_builder, "run_regime_batch", run_batch)
    regime_builder.compute_regime_incremental(MagicMock(), tmp_path)
    assert captured == [(LOCAL, BJ)]


def test_mainline_recompute_uses_beijing_today_for_both_kinds(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(regime_api, "_data_dir", lambda request: tmp_path)
    monkeypatch.setattr(regime_builder, "earliest_enriched_date", lambda repo: LOCAL)

    def compute(repo, data_dir, start, end, kind):
        captured.append((start, end, kind))
        return pl.DataFrame()

    monkeypatch.setattr(market_mainline, "compute_mainline_range", compute)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))
    assert regime_api.mainline_recompute(request)["ok"] is True
    assert captured == [(LOCAL, BJ, "concept"), (LOCAL, BJ, "industry")]


@pytest.mark.parametrize("future", [False, True])
def test_repair_http_checks_beijing_day_before_capability_guard(monkeypatch, future):
    app = FastAPI()
    app.include_router(kline_api.router)
    app.state.repo = MagicMock()
    app.state.capabilities = SimpleNamespace(has=lambda capability: False)
    monkeypatch.setattr(kline_api, "cn_today", lambda: BJ)
    # 本地日期故意比北京早一天; 有效北京日期应通过日期校验后进入能力检查。
    monkeypatch.setattr("datetime.date", _LocalDate)
    day = BJ + timedelta(days=1) if future else BJ
    with TestClient(app) as client:
        response = client.post("/api/kline/repair_daily", json={"start_date": day.isoformat()})
    assert response.status_code == (400 if future else 403), response.text
    if future:
        assert response.json()["detail"] == "起始日期不能晚于今天"
