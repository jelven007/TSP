"""A股业务日期缺省必须使用北京时间, 服务器时区不能成为隐式输入。"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl

from app.api import backtest as backtest_api
from app.api import factors as factors_api
from app.api import regime as regime_api
from app.services import market_mainline, regime_builder, repair_daily

BJ = date(2026, 3, 2)


@dataclass
class _DummyResult:
    status: str = "ok"


def test_backtest_run_defaults_to_beijing_today(monkeypatch) -> None:
    captured_cfg = []

    class DummyService:
        def __init__(self, repo):
            pass

        def run(self, cfg):
            captured_cfg.append(cfg)
            return _DummyResult()

    monkeypatch.setattr(backtest_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(backtest_api, "BacktestService", DummyService)

    request_data = backtest_api.BacktestRequest(symbols=["600000.SH"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    backtest_api.run(request_data, request)
    assert captured_cfg
    assert captured_cfg[0].end == BJ
    assert captured_cfg[0].start == BJ - timedelta(days=365 * 3)


def test_backtest_factor_run_defaults_to_beijing_today(monkeypatch) -> None:
    captured_cfg = []

    class DummyFactorService:
        def __init__(self, engine):
            pass

        def run(self, cfg):
            captured_cfg.append(cfg)
            return _DummyResult()

    monkeypatch.setattr(backtest_api, "cn_today", lambda: BJ)
    monkeypatch.setattr("app.backtest.factor.FactorBacktestService", DummyFactorService)
    monkeypatch.setattr(backtest_api, "_get_engine", lambda request: MagicMock())
    monkeypatch.setattr(
        "app.factors.registry.factor_columns_view",
        lambda: [{"id": "mom_20d"}],
    )

    request_data = backtest_api.FactorBacktestRequest(factor_name="mom_20d")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    backtest_api.factor_run(request_data, request)
    assert captured_cfg
    assert captured_cfg[0].end == BJ


def test_factors_trial_uses_beijing_today(monkeypatch) -> None:
    captured_args = []

    class DummyEngine:
        def load_panel(self, symbols, start, end, columns, asset_type):
            captured_args.append((start, end))
            return pl.DataFrame({column: [1.0] for column in columns})

    monkeypatch.setattr(factors_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(
        factors_api,
        "compile_formula",
        lambda formula: SimpleNamespace(
            ok=True,
            warmup_bars=5,
            dependencies=[],
            referenced_factors=[],
            frame_transform=lambda panel: panel,
        ),
    )
    monkeypatch.setattr("app.api.backtest._get_engine", lambda request: DummyEngine())
    monkeypatch.setattr(
        "app.backtest.factor.FactorBacktestService._compute_missing_factors",
        lambda panel, to_compute: panel,
    )
    monkeypatch.setattr("app.factors.dsl.FACTOR_COLUMN", "factor")

    request_data = factors_api.FormulaTrialRequest(formula="close / open", days=20)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    with suppress(Exception):
        factors_api.trial_formula(request_data, request)
    assert captured_args
    _start, end = captured_args[0]
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
