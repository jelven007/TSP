"""Reproducible default-strategy research using the production data/backtest APIs.

Run with PYTHONPATH=backend. All market data and detailed trades stay outside Git.
The snapshot is deliberately exploratory: current constituents are not a PIT universe.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import logging
import math
import shutil
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine
from app.backtest.matrix import MatrixPipelineConfig, MatrixStrategyPipeline
from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestConfig,
    StrategyBacktestService,
)
from app.data_providers import get_provider
from app.indicators.pipeline import run_pipeline
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository

WARMUP_START = "2020-10-01"
END = "2026-10-16"
WINDOWS = {
    "train": ("2021-10-17", "2024-10-16"),
    "validation": ("2024-10-17", "2025-10-16"),
    "holdout": ("2025-10-17", END),
    "five_year": ("2021-10-17", END),
}
RESEARCH_FAMILIES = {
    "trend": [
        "ma_golden_cross", "macd_golden", "bullish_alignment", "low_volatility_leader",
    ],
    "breakout": [
        "volume_price_surge", "boll_breakout", "breakout_new_high_60d",
        "platform_consolidation_breakout", "ma_convergence_breakout",
    ],
    "pullback": ["pullback_to_support", "pullback_ma20_bounce", "rsi_midline_pullback"],
    "reversal": [
        "n_day_low_reversal", "oversold_bounce", "oversold_reversal",
        "long_lower_shadow_reversal", "macd_below_zero_revival",
    ],
    "momentum": ["high_turnover_surge", "strong_open", "near_limit_up"],
    "limit": ["active_limit_gene", "consecutive_limit_ups"],
}
METADATA = (
    "instruments/instruments.parquet",
    "instrument_status/history.parquet",
    "financials/shares/part.parquet",
)


def finite_json(value):
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(finite_json(value), ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source: Path, target: Path, provider_name: str, batch_size: int) -> None:
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("研究快照必须与源目录隔离，不能嵌套或覆盖源目录")
    manifest_path = target / "research_snapshot.json"
    if target.exists() and any(target.iterdir()) and not manifest_path.exists():
        raise ValueError("拒绝写入非空且无研究标记的目录")
    target.mkdir(parents=True, exist_ok=True)
    identity = {
        "schema": 1,
        "provider": provider_name,
        "start": WARMUP_START,
        "end": END,
        "batch_size": batch_size,
        "metadata": {rel: sha256(source / rel) for rel in METADATA},
        "universe": "current_survivors_exploratory",
    }
    if manifest_path.exists():
        if json.loads(manifest_path.read_text())["identity"] != identity:
            raise ValueError("断点快照口径已变化，请使用新的研究目录")
    else:
        write_json(manifest_path, {"identity": identity, "state": "downloading"})
        for rel in METADATA:
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, target / rel)
    instruments = pl.read_parquet(target / METADATA[0])
    symbols = instruments["symbol"].unique().sort().to_list()
    provider = get_provider(provider_name)
    chunks = target / "_research_downloads"
    chunks.mkdir(exist_ok=True)
    start = datetime.fromisoformat(WARMUP_START)
    end = datetime.fromisoformat(END + "T23:59:59")
    try:
        for offset in range(0, len(symbols), batch_size):
            selected = symbols[offset:offset + batch_size]
            key = f"{offset // batch_size:04d}"
            for dataset in ("daily", "adj_factor"):
                out = chunks / f"{dataset}-{key}.parquet"
                receipt = out.with_suffix(".json")
                if out.exists() and receipt.exists():
                    saved = json.loads(receipt.read_text())
                    if saved["sha256"] != sha256(out) or saved["symbols"] != selected:
                        raise ValueError(f"断点校验失败: {out.name}")
                    continue
                for attempt in range(3):
                    try:
                        if dataset == "daily":
                            frame = provider.get_daily(selected, start, end)
                            missing = set(selected) - set(frame["symbol"].unique().to_list())
                            if missing:
                                raise ValueError(f"日线缺失: {sorted(missing)}")
                        else:
                            kwargs = {"strict": True} if provider_name == "rustdx" else {}
                            frame = provider.get_adj_factors(selected, start, end, **kwargs)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(1)
                KlineRepository._atomic_write_parquet(frame, out)
                write_json(receipt, {
                    "symbols": selected, "rows": frame.height, "sha256": sha256(out),
                })
            print(f"download {min(offset + batch_size, len(symbols))}/{len(symbols)}", flush=True)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    daily = pl.read_parquet(sorted(chunks.glob("daily-*.parquet")))
    factors = pl.read_parquet(sorted(chunks.glob("adj_factor-*.parquet")))
    store = DataStore(target)
    try:
        repo = KlineRepository(store)
        repo.append_daily(daily)
        KlineRepository._atomic_write_parquet(factors, target / "adj_factor/all.parquet")
    finally:
        store.db.close()
    print(f"persisted daily={daily.height} factors={factors.height}", flush=True)
    enriched_rows = run_pipeline(
        target, on_batch_done=lambda done, total: print(f"enriched {done}/{total}", flush=True),
    )
    coverage = daily.group_by("symbol").agg(
        pl.col("date").min().alias("first_date"),
        pl.col("date").max().alias("last_date"),
        pl.len().alias("bars"),
    ).sort("symbol")
    coverage.write_csv(target / "coverage.csv")
    write_json(manifest_path, {
        "identity": identity,
        "state": "ready",
        "symbols": len(symbols),
        "daily_rows": daily.height,
        "enriched_rows": enriched_rows,
        "factor_rows": factors.height,
        "factor_symbols": factors["symbol"].n_unique(),
        "dates": daily["date"].n_unique(),
        "first_date": daily["date"].min(),
        "last_date": daily["date"].max(),
        "cover_start_symbols": coverage.filter(pl.col("first_date") <= datetime(2021, 10, 18).date()).height,
        "download_hashes": {p.name: sha256(p) for p in sorted(chunks.glob("*.parquet"))},
        "limitations": [
            "当前在市股票池，未覆盖历史退市证券，存在生存者偏差",
            "历史证券状态首次采集前未知，当前ST名称仅兼容降级",
            "早期历史股本缺失时流水线使用当前股本，换手/市值有时点偏差",
            "上市日期缺失时无法精确复原新股无涨跌幅限制窗口",
        ],
    })


def trade_metrics(trades: list[dict]) -> dict:
    pnls = np.array([trade["pnl_pct"] for trade in trades], dtype=float)
    amounts = np.array([trade["pnl_amount"] for trade in trades], dtype=float)
    n = len(pnls)
    if not n:
        return {"trades": 0, "win_rate": None, "expectancy": None, "profit_factor": None}
    wins = int((pnls > 0).sum())
    rate = wins / n
    z = 1.96
    centre = (rate + z * z / (2 * n)) / (1 + z * z / n)
    width = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    gain = amounts[amounts > 0].sum()
    loss = -amounts[amounts < 0].sum()
    return {
        "trades": n,
        "win_rate": rate,
        "win_rate_wilson95": [centre - width, centre + width],
        "expectancy": float(pnls.mean()),
        "profit_factor": float(gain / loss) if loss else None,
        "payoff_ratio": float(pnls[pnls > 0].mean() / -pnls[pnls < 0].mean())
        if (pnls > 0).any() and (pnls < 0).any() else None,
        "net_pnl": float(amounts.sum()),
    }


def candidate_trials() -> list[dict]:
    """Exactly two registered hypotheses per strategy; no adaptive grid expansion."""
    trials = []
    for family, strategy_ids in RESEARCH_FAMILIES.items():
        for strategy_id in strategy_ids:
            if family in {"trend", "breakout"}:
                conditions = [
                    {"left": "ma20", "op": ">=", "right": "field:ma60"},
                    {"left": "ma60", "op": ">=", "right": "field:ma60", "rightDays": 5},
                ]
            elif family in {"pullback", "reversal"}:
                conditions = [
                    {"left": "close", "op": ">", "right": "field:close", "rightDays": 1},
                    {"left": "close", "op": ">", "right": "field:ma5"},
                ]
            elif family == "momentum":
                conditions = [
                    {"left": "annual_vol_20d", "op": "<=", "right": threshold}
                    for threshold in ("0.6", "0.8")
                ]
            elif strategy_id == "active_limit_gene":
                conditions = [
                    {"left": "close", "op": ">", "right": "field:ma20"},
                    {"left": "close", "op": ">", "right": "field:close", "rightDays": 1},
                ]
            else:
                conditions = [
                    {"left": "consecutive_limit_ups", "op": "<=", "right": threshold}
                    for threshold in ("2", "3")
                ]
            for variant, condition in zip(("A", "B"), conditions, strict=True):
                trials.append({
                    "id": f"{family}_{variant}", "strategy_id": strategy_id,
                    "overrides": {"overlay_filter": [condition]},
                })
    return trials


def enrich_metadata(data: Path) -> None:
    """Repair only the marked research snapshot through the production sync chain."""
    from app.services import instrument_sync, preferences

    manifest_path = data / "research_snapshot.json"
    manifest = json.loads(manifest_path.read_text())
    if preferences.get_daily_data_provider() != manifest["identity"]["provider"]:
        raise ValueError("标的同步路由与研究数据源不同")
    before = pl.read_parquet(data / METADATA[0])
    manifest["state"] = "enriching_metadata"
    write_json(manifest_path, manifest)
    instrument_sync.sync_instruments(data)
    after = pl.read_parquet(data / METADATA[0])
    if set(before["symbol"]) != set(after["symbol"]):
        raise ValueError("证券池已变化，必须重新登记数据快照")
    manifest["metadata_coverage"] = {
        key: after[key].is_not_null().sum()
        for key in ("listing_date", "total_shares", "float_shares")
    }
    if not all(manifest["metadata_coverage"].values()):
        raise ValueError("上市日期或股本仍完全缺失")
    print(f"metadata {manifest['metadata_coverage']}", flush=True)
    run_pipeline(
        data, on_batch_done=lambda done, total: print(f"enriched {done}/{total}", flush=True),
    )
    manifest["enriched_metadata_hashes"] = {rel: sha256(data / rel) for rel in METADATA}
    manifest["state"] = "ready"
    write_json(manifest_path, manifest)


def run_trials(data: Path, output: Path, phase: str, trial_file: Path | None) -> None:
    manifest = json.loads((data / "research_snapshot.json").read_text())
    if manifest["state"] != "ready":
        raise ValueError("研究数据未就绪")
    builtin_dir = data / "_baseline_strategy_code"
    if not builtin_dir.exists():
        source = Path(__file__).resolve().parents[1] / "backend/app/strategy/builtin"
        shutil.copytree(source, builtin_dir, ignore=shutil.ignore_patterns("__pycache__"))
    registry = StrategyEngine([builtin_dir])
    if len(registry.list_strategies()) != 22:
        raise ValueError("默认策略数量不是 22，请检查加载错误或重新登记研究范围")
    trials = json.loads(trial_file.read_text()) if trial_file else [
        {"id": "baseline", "strategy_id": item["id"]}
        for item in sorted(registry.list_strategies(), key=lambda item: item["id"])
    ]
    if phase in {"holdout", "five_year"} and (
        trial_file is None or trial_file.name != "finalists.json"
    ):
        raise ValueError("留出期及五年终审只能使用定型后封存的 finalists.json")
    output.mkdir(parents=True, exist_ok=True)
    begin, end = (date.fromisoformat(value) for value in WINDOWS[phase])
    store = DataStore(data, read_only=True)
    policy = BacktestResultPolicy(
        include_monte_carlo=False, include_per_symbol_stats=False,
        include_benchmark=False, include_return_distribution=False,
    )
    try:
        service = StrategyBacktestService(BacktestEngine(KlineRepository(store)), registry)
        for trial in trials:
            strategy_id = trial["strategy_id"]
            strategy = registry.get(strategy_id)
            out = output / f"{phase}__{strategy_id}__{trial['id']}.json"
            identity = {
                "trial": trial, "phase": phase,
                "snapshot_hash": sha256(data / "research_snapshot.json"),
                "strategy_source_hash": sha256(strategy.file_path),
            }
            if out.exists():
                if json.loads(out.read_text())["research_identity"] != identity:
                    raise ValueError(f"拒绝覆盖不同口径研究结果: {out.name}")
                print(f"cached {out.stem}", flush=True)
                continue
            config = StrategyBacktestConfig(
                strategy_id=strategy_id, symbols=None, start=begin, end=end,
                params=trial.get("params"), overrides=trial.get("overrides"),
            )
            prepared = service.prepare_matrix_optimization(
                [config], matrix_cache_max_bytes=256 * 1024 * 1024,
            )
            try:
                result = service.run(config, prepared=prepared, result_policy=policy)
                detail = asdict(result)
                if result.error and result.error != "在指定区间内未产生买入信号":
                    raise ValueError(f"{strategy_id}: {result.error}")
                detail["research_identity"] = identity
                detail["metrics"] = trade_metrics(result.trades)
                detail["metrics_excluding_window_end"] = trade_metrics([
                    trade for trade in result.trades if trade["exit_reason"] != "end"
                ])
                detail["by_exit_year"] = {
                    str(year): trade_metrics([
                        trade for trade in result.trades
                        if str(trade["exit_date"]).startswith(str(year))
                    ])
                    for year in range(begin.year, end.year + 1)
                }
                # Signal overlap uses the same filtering/scoring pipeline as the run.
                if trial["id"] == "baseline" and phase in {"train", "validation"}:
                    with prepared.compute_cache.activate(prepared.market_data):
                        signals = MatrixStrategyPipeline().run(
                            strategy.matrix_strategy, prepared.market_data,
                            service._normalize_params(config.params or {}, strategy),
                            MatrixPipelineConfig(
                                basic_filter=strategy.basic_filter,
                                scoring=strategy.meta.get("scoring") or {},
                                order_by=strategy.meta.get("order_by"),
                                descending=bool(strategy.meta.get("descending", True)),
                                protect_strategy_cache=True,
                            ),
                        )
                    visible = signals.entry[prepared.start_id:prepared.stop_id]
                    np.savez_compressed(
                        out.with_suffix(".npz"),
                        entries=np.flatnonzero(visible),
                        shape=visible.shape,
                        symbols=prepared.market_data.symbols,
                        dates=prepared.market_data.timestamp_labels[prepared.start_id:prepared.stop_id],
                    )
                    del signals, visible
                write_json(out, detail)
                metrics = detail["metrics"]
                print(
                    f"{out.stem}: trades={metrics['trades']} win={metrics['win_rate']} "
                    f"mean={metrics['expectancy']} seconds={result.elapsed_ms / 1000:.1f}",
                    flush=True,
                )
            finally:
                prepared.compute_cache.close()
                del prepared
                gc.collect()
    finally:
        store.db.close()


def summarize(output: Path, report: Path) -> None:
    rows = []
    for path in sorted(output.glob("*__*__*.json")):
        result = json.loads(path.read_text())
        identity = result["research_identity"]
        metrics = result["metrics"]
        stats = result["stats"]
        rows.append({
            "phase": identity["phase"],
            "strategy_id": identity["trial"]["strategy_id"],
            "trial_id": identity["trial"]["id"],
            **{key: metrics.get(key) for key in (
                "trades", "win_rate", "expectancy", "profit_factor", "payoff_ratio", "net_pnl",
            )},
            **{key: stats.get(key) for key in (
                "total_return", "max_drawdown", "sharpe", "avg_exposure", "pending_exit_positions",
            )},
            "natural_exit_win_rate": result["metrics_excluding_window_end"]["win_rate"],
            "error": result["error"],
        })
    report.mkdir(parents=True, exist_ok=True)
    if rows:
        pl.from_dicts(rows, infer_schema_length=None).write_csv(report / "metrics.csv")
    overlap = []
    for phase in ("train", "validation"):
        paths = sorted(output.glob(f"{phase}__*__baseline.npz"))
        for index, left_path in enumerate(paths):
            with np.load(left_path) as left:
                for right_path in paths[index + 1:]:
                    with np.load(right_path) as right:
                        for key in ("shape", "symbols", "dates"):
                            if not np.array_equal(left[key], right[key]):
                                raise ValueError(f"信号坐标不一致: {left_path.name}/{right_path.name}")
                        a, b = left["entries"], right["entries"]
                        intersection = len(np.intersect1d(a, b, assume_unique=True))
                        union = len(a) + len(b) - intersection
                        overlap.append({
                            "phase": phase,
                            "left": left_path.stem.split("__")[1],
                            "right": right_path.stem.split("__")[1],
                            "left_signals": len(a), "right_signals": len(b),
                            "intersection": intersection,
                            "jaccard": intersection / union if union else None,
                            "left_covered": intersection / len(a) if len(a) else None,
                            "right_covered": intersection / len(b) if len(b) else None,
                        })
    if overlap:
        pl.from_dicts(overlap, infer_schema_length=None).write_csv(report / "signal_overlap.csv")


def audit_data(data: Path) -> None:
    """Detect coverage gaps and suspicious adjusted bars without changing the sample."""
    root = (data / "kline_daily_enriched/**/*.parquet").as_posix()
    frame = pl.scan_parquet(root, hive_partitioning=False).select(
        "symbol", "date", "open", "high", "low", "close", "volume", "amount",
    ).sort(["symbol", "date"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("return"),
        pl.col("date").cum_count().over("symbol").alias("observed_bars"),
    ).collect()
    invalid = frame.filter(
        (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") <= 0)
        | (pl.col("volume") < 0)
        | (pl.col("amount") < 0)
    )
    suspicious = frame.filter(
        (pl.col("observed_bars") > 60)
        & (pl.col("return").abs() > 0.22)
    )
    suspicious.select("symbol", "date", "return").write_csv(data / "suspicious_adjusted_returns.csv")
    write_json(data / "data_audit.json", {
        "observations": frame.height,
        "invalid_ohlcv_rows": invalid.height,
        "large_adjusted_return_rows_after_60_bars": suspicious.height,
        "large_adjusted_return_symbols": suspicious["symbol"].n_unique(),
        "year_coverage": frame.group_by(pl.col("date").dt.year().alias("year")).agg(
            pl.len().alias("rows"), pl.col("date").n_unique().alias("trading_days"),
            pl.col("symbol").n_unique().alias("symbols"),
        ).sort("year").to_dicts(),
        "policy": "异常仅标记，不依据事后收益删除证券；终审检查对应成交影响",
    })


def rejection_reasons(candidate: dict, baseline: dict, phase: str) -> list[str]:
    current, original = candidate["metrics"], baseline["metrics"]
    reasons = []
    for field in ("win_rate", "expectancy", "profit_factor"):
        value, base = current.get(field), original.get(field)
        if value is None or base is None:
            reasons.append(f"{field}_unavailable")
        elif value < base or (field == "win_rate" and value == base):
            reasons.append(f"{field}_not_improved")
    if phase == "validation":
        if current["trades"] < 100:
            reasons.append("fewer_than_100_trades")
        if current["trades"] < original["trades"] * 0.5:
            reasons.append("fewer_than_half_baseline_trades")
        drawdown = candidate["stats"].get("max_drawdown")
        baseline_drawdown = baseline["stats"].get("max_drawdown")
        if drawdown is None or baseline_drawdown is None or drawdown < baseline_drawdown:
            reasons.append("drawdown_worse_or_unavailable")
    return reasons


def select_trials(output: Path, phase: str, target: Path) -> None:
    passed = []
    decisions = []
    for path in sorted(output.glob(f"{phase}__*.json")):
        candidate = json.loads(path.read_text())
        trial = candidate["research_identity"]["trial"]
        if trial["id"] == "baseline":
            continue
        baseline_path = output / f"{phase}__{trial['strategy_id']}__baseline.json"
        baseline = json.loads(baseline_path.read_text())
        reasons = rejection_reasons(candidate, baseline, phase)
        decisions.append({"trial": trial, "rejected": reasons, "evidence_sha256": sha256(path)})
        if not reasons:
            passed.append(trial)
    write_json(target, passed)
    write_json(target.with_name(target.stem + "-decisions.json"), decisions)
    print(f"{phase}: {len(passed)}/{len(decisions)} passed", flush=True)


def monthly_block_comparison(
    baseline: list[dict], candidate: list[dict], *, draws: int = 10_000, seed: int = 20261017,
) -> dict:
    """Paired calendar-month bootstrap; never pretend correlated trades are IID."""
    months = sorted({t["entry_date"][:7] for t in baseline + candidate})
    if not months:
        return {"months": 0, "win_rate_delta95": None, "expectancy_delta95": None}

    def grouped(trades):
        return np.array([
            [
                sum(t["entry_date"].startswith(month) for t in trades),
                sum(t["entry_date"].startswith(month) and t["pnl_pct"] > 0 for t in trades),
                sum(t["pnl_pct"] for t in trades if t["entry_date"].startswith(month)),
            ]
            for month in months
        ], dtype=float)

    weights = np.random.default_rng(seed).multinomial(
        len(months), [1 / len(months)] * len(months), size=draws,
    )
    original, current = weights @ grouped(baseline), weights @ grouped(candidate)
    valid = (original[:, 0] > 0) & (current[:, 0] > 0)
    deltas = current[valid, 1:] / current[valid, :1] - original[valid, 1:] / original[valid, :1]
    return {
        "months": len(months), "draws": draws, "seed": seed,
        "win_rate_delta95": np.quantile(deltas[:, 0], [0.025, 0.975]).tolist()
        if valid.any() else None,
        "expectancy_delta95": np.quantile(deltas[:, 1], [0.025, 0.975]).tolist()
        if valid.any() else None,
        "note": "按相同入场月份权重重采样; 未校正多重尝试, 不代表独立交易显著性",
    }


def stability(output: Path, trials: Path, target: Path) -> None:
    evidence = []
    for trial in json.loads(trials.read_text()):
        sid = trial["strategy_id"]
        for phase in ("train", "validation"):
            def result(trial_id):
                return json.loads((output / f"{phase}__{sid}__{trial_id}.json").read_text())

            base, candidate = result("baseline"), result(trial["id"])
            evidence.append({
                "phase": phase, "strategy_id": sid,
                "bootstrap": monthly_block_comparison(base["trades"], candidate["trades"]),
                "by_exit_year": {
                    year: {
                        "baseline": base["by_exit_year"][year],
                        "candidate": candidate["by_exit_year"][year],
                    } for year in base["by_exit_year"]
                },
                "neighbors": {
                    name: {
                        "metrics": result(name)["metrics"],
                        "rejected": rejection_reasons(result(name), base, phase),
                    } for name in ("stability_3d", "stability_7d")
                },
            })
    write_json(target, evidence)


def audit_trades(data: Path, output: Path, report: Path) -> None:
    events = pl.read_csv(data / "suspicious_adjusted_returns.csv").to_dicts()
    impacts, annual, overlaps = [], [], []
    for path in sorted(output.glob("*__*__*.json")):
        result = json.loads(path.read_text())
        trial = result["research_identity"]["trial"]
        phase = result["research_identity"]["phase"]
        if phase not in {"holdout", "five_year"}:
            continue
        affected = [
            t for t in result["trades"]
            if any(
                event["symbol"] == t["symbol"]
                and t["entry_date"] <= event["date"] <= t["exit_date"]
                for event in events
            )
        ]
        impacts.append({
            "phase": phase, "strategy_id": trial["strategy_id"], "trial_id": trial["id"],
            "affected_trades": len(affected),
            "all": result["metrics"],
            "excluding_flagged_trades_sensitivity_only": trade_metrics([
                t for t in result["trades"] if t not in affected
            ]),
        })
        for year, metrics in result["by_exit_year"].items():
            annual.append({
                "phase": phase, "strategy_id": trial["strategy_id"], "trial_id": trial["id"],
                "exit_year": year,
                **{k: metrics.get(k) for k in (
                    "trades", "win_rate", "expectancy", "profit_factor", "net_pnl",
                )},
            })
    paths = sorted(output.glob("five_year__*__baseline.json"))
    trades = {
        p.stem.split("__")[1]: {
            (t["symbol"], t["entry_date"]) for t in json.loads(p.read_text())["trades"]
        } for p in paths
    }
    for index, left in enumerate(trades):
        for right in list(trades)[index + 1:]:
            a, b = trades[left], trades[right]
            overlaps.append({
                "left": left, "right": right, "intersection": len(a & b),
                "union": len(a | b), "jaccard": len(a & b) / len(a | b) if a | b else None,
            })
    report.mkdir(parents=True, exist_ok=True)
    write_json(report / "anomaly_trade_impact.json", impacts)
    if annual:
        pl.from_dicts(annual, infer_schema_length=None).write_csv(report / "annual_metrics.csv")
    if overlaps:
        pl.from_dicts(overlaps, infer_schema_length=None).write_csv(report / "trade_overlap.csv")


def verify_implementation(data: Path, output: Path, target: Path) -> None:
    """Compare shipped code against frozen five-year trial trades, not just summary stats."""
    source = Path(__file__).resolve().parents[1] / "backend/app/strategy/builtin"
    registry = StrategyEngine([source])
    cases = [
        (sid, {}, "baseline") for sid in (
            "oversold_bounce", "oversold_reversal", "pullback_to_support", "pullback_ma20_bounce",
        )
    ] + [
        ("ma_golden_cross", {}, "trend_B"),
        ("ma_golden_cross", {"require_ma60_non_declining": False}, "baseline"),
    ]
    store = DataStore(data, read_only=True)
    evidence = []
    try:
        service = StrategyBacktestService(BacktestEngine(KlineRepository(store)), registry)
        for sid, params, reference_id in cases:
            config = StrategyBacktestConfig(
                strategy_id=sid, symbols=None,
                start=date.fromisoformat(WINDOWS["five_year"][0]),
                end=date.fromisoformat(END), params=params,
            )
            prepared = service.prepare_matrix_optimization([config], matrix_cache_max_bytes=256 << 20)
            try:
                result = service.run(config, prepared=prepared, result_policy=BacktestResultPolicy(
                    include_monte_carlo=False, include_per_symbol_stats=False,
                    include_benchmark=False, include_return_distribution=False,
                ))
                reference_path = output / f"five_year__{sid}__{reference_id}.json"
                reference = json.loads(reference_path.read_text())
                passed = result.trades == reference["trades"] and all(
                    result.stats.get(k) == reference["stats"].get(k)
                    for k in ("total_return", "max_drawdown", "pending_exit_positions")
                )
                evidence.append({
                    "strategy_id": sid, "params": params, "reference_id": reference_id,
                    "reference_sha256": sha256(reference_path),
                    "trades": len(result.trades), "exact_trade_and_stats_match": passed,
                    "error": result.error,
                })
                print(f"parity {sid} {reference_id}: {passed}", flush=True)
            finally:
                prepared.compute_cache.close()
                del prepared
                gc.collect()
    finally:
        store.db.close()
    write_json(target, evidence)
    if not all(item["exact_trade_and_stats_match"] for item in evidence):
        raise ValueError("新实现未精确复现冻结候选或原版成交，必须调查差异")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--data", type=Path, required=True)
    prep.add_argument("--provider", default="rustdx")
    prep.add_argument("--batch-size", type=int, default=160)
    enrich = sub.add_parser("enrich-metadata")
    enrich.add_argument("--data", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--data", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--phase", choices=WINDOWS, required=True)
    run.add_argument("--trials", type=Path)
    candidates = sub.add_parser("candidates")
    candidates.add_argument("--output", type=Path, required=True)
    summary = sub.add_parser("summarize")
    summary.add_argument("--output", type=Path, required=True)
    summary.add_argument("--report", type=Path, required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--data", type=Path, required=True)
    select = sub.add_parser("select")
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--phase", choices=("train", "validation"), required=True)
    select.add_argument("--target", type=Path, required=True)
    stable = sub.add_parser("stability")
    stable.add_argument("--output", type=Path, required=True)
    stable.add_argument("--trials", type=Path, required=True)
    stable.add_argument("--target", type=Path, required=True)
    impact = sub.add_parser("audit-trades")
    impact.add_argument("--data", type=Path, required=True)
    impact.add_argument("--output", type=Path, required=True)
    impact.add_argument("--report", type=Path, required=True)
    parity = sub.add_parser("verify-implementation")
    parity.add_argument("--data", type=Path, required=True)
    parity.add_argument("--output", type=Path, required=True)
    parity.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.command == "prepare":
        if args.batch_size < 1:
            parser.error("batch-size must be positive")
        prepare(args.source, args.data, args.provider, args.batch_size)
    elif args.command == "run":
        run_trials(args.data, args.output, args.phase, args.trials)
    elif args.command == "enrich-metadata":
        enrich_metadata(args.data)
    elif args.command == "candidates":
        write_json(args.output, candidate_trials())
    elif args.command == "summarize":
        summarize(args.output, args.report)
    elif args.command == "audit":
        audit_data(args.data)
    elif args.command == "select":
        select_trials(args.output, args.phase, args.target)
    elif args.command == "stability":
        stability(args.output, args.trials, args.target)
    elif args.command == "audit-trades":
        audit_trades(args.data, args.output, args.report)
    elif args.command == "verify-implementation":
        verify_implementation(args.data, args.output, args.target)


if __name__ == "__main__":
    main()
