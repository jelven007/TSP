"""Fair, serial research of the registered quality-entry hypotheses.

PYTHONPATH=backend python scripts/research/research_quality_strategies.py run --data ... --output ...
Never writes the input snapshot. Old studies remain untouched. Results are exploratory.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine
from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    load_market_data_matrix_from_parquet,
)
from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestConfig,
    StrategyBacktestService,
    _strategy_definition_hash,
    build_matrix_cache_profile,
)
from app.strategy.builtin._quality_entries import ENTRY_RULE_VERSION, VARIANTS
from app.strategy.engine import StrategyEngine
from app.strategy.entry_risk import ENTRY_RISK_VERSION
from app.strategy.families import BUILTIN_FAMILIES
from app.tickflow.repository import DataStore, KlineRepository
from research_default_strategies import (
    monthly_block_comparison,
    sha256,
    trade_metrics,
    write_json,
)

ROOT = Path(__file__).resolve().parents[2]
PRIORITY = [
    "n_day_low_reversal", "long_lower_shadow_reversal", "oversold_reversal",
    "macd_below_zero_revival", "pullback_ma20_bounce", "platform_consolidation_breakout",
]
PENDING_TOPICS = {
    "low_volatility_leader": "匹配宽基60日相对收益：冻结输入缺少指数历史，未试验",
    "strong_open": "成交溢价上限已登记；9:35承接仍缺完整分钟覆盖",
    "consecutive_limit_ups": "保留接力观察定义；一进二缺少冻结首板池及分钟承接数据，未试验",
}
RISK_VARIANTS = {
    "execution_premium_3pct": {"max_entry_premium_pct": .03},
    "execution_premium_5pct": {"max_entry_premium_pct": .05},
    "risk_atr_2": {"stop_atr_multiple": 2.},
    "risk_signal_low": {"stop_signal_low": True},
    "risk_budget_0p5pct": {"risk_per_trade_pct": .005},
}


def variants_for(sid):
    entry = [item.id for item in VARIANTS.get(sid, ())]
    risk = ["risk_atr_2", "risk_signal_low", "risk_budget_0p5pct"] if sid in PRIORITY else []
    if sid == "strong_open":
        risk = ["execution_premium_3pct", "execution_premium_5pct"]
    return ["original", *entry, *risk]


def trial_variant(config):
    risk = (config.overrides or {}).get("entry_risk")
    if risk:
        return next(name for name, values in RISK_VARIANTS.items() if risk == values)
    return (config.params or {}).get("entry_variant", "original")


def source_manifest() -> dict:
    paths = sorted((ROOT / "backend/app").rglob("*.py"))
    paths += [Path(__file__), ROOT / "scripts/research/research_default_strategies.py"]
    return {str(path.relative_to(ROOT)): sha256(path) for path in paths}


def input_manifest(data: Path) -> dict:
    paths = [data / "research_snapshot.json"]
    for folder in ("kline_daily_enriched", "instruments", "instrument_status", "financials/shares"):
        paths.extend(sorted((data / folder).rglob("*.parquet")))
    result = {}
    for index, path in enumerate(paths):
        result[str(path.relative_to(data))] = sha256(path)
        if index % 250 == 0:
            print(f"input hashes {index + 1}/{len(paths)}", flush=True)
    return result


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _config(args, sid, variant, symbols):
    risk = RISK_VARIANTS.get(variant)
    return StrategyBacktestConfig(
        strategy_id=sid, symbols=symbols, start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        params={"entry_variant": "original" if risk else variant} if sid in VARIANTS else {},
        overrides={"entry_risk": dict(risk)} if risk else {},
        matching="open_t+1", entry_fill="open_t+1", exit_fill="open_t+1",
        commission_pct=.0002, stamp_tax_pct=None, minimum_commission=5.,
        transfer_fee_pct=.00001, slippage_bps=args.slippage_bps,
        max_positions=10, max_exposure_pct=1., initial_capital=1_000_000.,
        position_sizing="equal", mode="position", strict_research=False,
    )


def _groups(trades, key):
    labels = sorted({str(key(trade)) for trade in trades})
    return {label: trade_metrics([t for t in trades if str(key(t)) == label]) for label in labels}


def diagnostics(trades):
    """Grouping is diagnostic, never a retrospective universe filter."""
    by_symbol = _groups(trades, lambda t: t["symbol"])
    top = sorted(by_symbol, key=lambda s: by_symbol[s].get("net_pnl", 0), reverse=True)[:5]
    by_month = _groups(trades, lambda t: t["entry_date"][:7])
    months = sorted(by_month, key=lambda m: by_month[m].get("net_pnl", 0), reverse=True)[:5]
    return {
        "by_entry_year": _groups(trades, lambda t: t["entry_date"][:4]),
        "by_entry_month": by_month,
        "by_exit_reason": _groups(trades, lambda t: t["exit_reason"]),
        "top_five_symbols": {s: by_symbol[s] for s in top},
        "top_five_months": {m: by_month[m] for m in months},
        "excluding_top_five_symbols": trade_metrics([t for t in trades if t["symbol"] not in top]),
        "excluding_2025_entries": trade_metrics([t for t in trades if not t["entry_date"].startswith("2025")]),
        "natural_exits": trade_metrics([t for t in trades if t["exit_reason"] != "end"]),
    }


def run(args):
    data, output = args.data.resolve(), args.output.resolve()
    if data == output or data in output.parents or output in data.parents:
        raise ValueError("结果目录必须与冻结输入隔离，不能嵌套")
    snapshot = json.loads((data / "research_snapshot.json").read_text())
    if snapshot["state"] != "ready":
        raise ValueError("研究输入未就绪")
    if output.exists() and any(output.iterdir()) and not (output / "identity.json").exists():
        raise ValueError("拒绝使用非空且没有研究标记的目录")
    registry = StrategyEngine([ROOT / "backend/app/strategy/builtin"])
    all_ids = [sid for _, members in BUILTIN_FAMILIES.values() for sid in members]
    if len(registry.list_strategies()) != 22:
        raise ValueError("策略加载未完整保留22项")
    ordered = PRIORITY + [sid for sid in all_ids if sid not in PRIORITY]
    ids = [sid for sid in ordered if not args.strategies or sid in args.strategies]
    if args.strategies and set(args.strategies) - set(all_ids):
        raise ValueError("请求了未知策略")
    symbols = None
    if args.max_symbols:
        symbols = sorted(pl.read_parquet(data / "instruments/instruments.parquet")["symbol"].unique())[
            :args.max_symbols
        ]
    configs = {
        sid: [_config(args, sid, v, symbols) for v in variants_for(sid)]
        for sid in ids
    }
    identity = {
        "schema": 2, "rule_version": ENTRY_RULE_VERSION, "entry_risk_version": ENTRY_RISK_VERSION,
        "source": source_manifest(),
        "inputs": input_manifest(data),
        "configs": {sid: [asdict(c) for c in group] for sid, group in configs.items()},
        "data_limitations": snapshot.get("limitations", []),
        "interpretation": "已研究历史上的开发诊断；不是新独立留出，不自动晋级生产默认",
        "fee_assumption": "研究假设：佣金双边2bp/每边最低5元/额外过户0.1bp；未代表实际券商费表",
        "universe": "sorted_symbol_smoke" if symbols else "current_survivors_exploratory",
        "pending_topics": PENDING_TOPICS,
        "previous_trials": {"known_primary_attempts": 44, "additional_attempts": "not_reset_unknown"},
    }
    identity_hash = _digest(identity)
    if (output / "identity.json").exists():
        saved = json.loads((output / "identity.json").read_text())
        if saved["sha256"] != identity_hash:
            raise ValueError("代码、参数或输入已变化，请使用新结果目录")
    else:
        write_json(output / "identity.json", {"sha256": identity_hash, "identity": identity})
        # Archive exactly the code whose hashes were recorded, including uncommitted work.
        for relative in identity["source"]:
            target = output / "source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
    policy = BacktestResultPolicy(
        include_monte_carlo=False, include_per_symbol_stats=False,
        include_return_distribution=False, include_benchmark=False,
    )
    store = DataStore(data, read_only=True)
    try:
        repo = KlineRepository(store)
        service = StrategyBacktestService(BacktestEngine(repo), registry)
        profile = build_matrix_cache_profile(registry, "stock")
        fields = profile.field_columns | {"atr_14"}
        # Use the production loader with all builtin fields. Keep its disposable
        # mmap cache in the result directory, never inside the frozen input.
        master = load_market_data_matrix_from_parquet(
            data / "kline_daily_enriched", date.fromisoformat(args.start) - timedelta(days=365),
            date.fromisoformat(args.end), field_columns=fields, symbols=symbols,
            instruments=repo.get_instruments_asset("stock"),
            instrument_history=repo.get_instrument_history(),
            cache_root=output / "matrix-cache", cache_field_columns=fields,
            cache_max_bytes=1024 * 1024 * 1024, profile_generation=profile.generation,
            source_generation=repo.get_matrix_data_generation("stock"),
        )
        print(f"shared matrix {master.shape}, {master.nbytes / 1024**2:.1f} MiB", flush=True)
        for sid in ids:
            prepared = None
            prepared_overrides = None
            try:
                for config in configs[sid]:
                    variant = trial_variant(config)
                    path = output / "results" / f"{sid}__{variant}.json"
                    if path.exists():
                        saved = json.loads(path.read_text())
                        if saved["identity_sha256"] != identity_hash:
                            raise ValueError(f"断点身份不一致: {path.name}")
                        print(f"cached {path.stem}", flush=True)
                        continue
                    if prepared is None or prepared_overrides != config.overrides:
                        if prepared is not None:
                            prepared.compute_cache.close()
                        # The production optimizer intentionally requires identical
                        # overrides. Share the input matrix, but never merge policies.
                        group = [trial for trial in configs[sid] if trial.overrides == config.overrides]
                        prepared = service.prepare_matrix_optimization(
                            group, market_data_override=master,
                            matrix_cache_max_bytes=128 * 1024 * 1024,
                        )
                        prepared_overrides = config.overrides
                    print(f"running {path.stem}", flush=True)
                    result = service.run(
                        config, prepared=prepared, result_policy=policy,
                        progress_cb=lambda update: print(f"progress {path.stem}: {update}", flush=True),
                    )
                    if result.error and result.error != "在指定区间内未产生买入信号":
                        raise ValueError(f"{sid}: {result.error}")
                    definition = registry.get(sid)
                    params = service._normalize_params(config.params or {}, definition)
                    with prepared.compute_cache.activate(prepared.market_data):
                        signals = MatrixStrategyPipeline().run(
                            definition.matrix_strategy, prepared.market_data, params,
                            MatrixPipelineConfig(
                                basic_filter=definition.basic_filter, scoring=definition.meta.get("scoring") or {},
                                order_by=definition.meta.get("order_by"),
                                descending=bool(definition.meta.get("descending", True)),
                                protect_strategy_cache=True,
                            ),
                        )
                    visible = signals.entry[prepared.start_id:prepared.stop_id]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    np.savez_compressed(
                        path.with_suffix(".npz"), entries=np.flatnonzero(visible), shape=visible.shape,
                        dates=prepared.market_data.timestamp_labels[prepared.start_id:prepared.stop_id],
                        symbols=prepared.market_data.symbols,
                    )
                    detail = asdict(result)
                    detail.update({
                        "identity_sha256": identity_hash, "definition_sha256": _strategy_definition_hash(definition, registry),
                        "resolved_params": params, "entry_signals": int(visible.sum()),
                        "metrics": trade_metrics(result.trades), "diagnostics": diagnostics(result.trades),
                    })
                    if source_manifest() != identity["source"]:
                        raise ValueError("运行期间源码变化，拒绝把不同版本记入同一批次")
                    write_json(path, detail)
                    print(f"done {path.stem}: {detail['metrics']} seconds={result.elapsed_ms / 1000:.1f}", flush=True)
                    del signals, visible, result, detail
                    gc.collect()
            finally:
                if prepared is not None:
                    prepared.compute_cache.close()
    finally:
        store.db.close()
    write_json(output / "completed.json", {"identity_sha256": identity_hash, "strategies": ids})


def _differences(baseline, candidate):
    def trade_key(t):
        return t["symbol"], t["entry_date"]
    a, b = ({trade_key(t): t for t in item["trades"]} for item in (baseline, candidate))
    removed, added = a.keys() - b.keys(), b.keys() - a.keys()
    return {
        "same_stock_entry_day": len(a.keys() & b.keys()),
        "missed_winners": sum(a[k]["pnl_amount"] > 0 for k in removed),
        "avoided_losers": sum(a[k]["pnl_amount"] < 0 for k in removed),
        "removed_metrics": trade_metrics([a[k] for k in removed]),
        "added_metrics": trade_metrics([b[k] for k in added]),
        "note": "账户名额和排序会改变交易集合；不是匹配交易的因果估计",
    }


def candidate_decision(baseline, candidate, *, objective="win_rate"):
    a, b = baseline["metrics"], candidate["metrics"]
    reasons = []
    for metric in ("win_rate", "expectancy", "profit_factor"):
        if a.get(metric) is None or b.get(metric) is None:
            reasons.append(f"{metric}_unavailable")
        elif b[metric] < a[metric] + (.03 if metric == "win_rate" and objective == "win_rate" else 0):
            reasons.append(f"{metric}_below_registered_gate")
    if b["trades"] < a["trades"] * .5:
        reasons.append("coverage_below_half")
    for metric in ("total_return", "max_drawdown"):
        original, current = baseline["stats"].get(metric), candidate["stats"].get(metric)
        if original is None or current is None or not np.isfinite([original, current]).all():
            reasons.append(f"{metric}_unavailable")
        elif current < original:
            reasons.append(f"{metric}_worse")
    if objective == "risk_return":
        before, after = baseline["stats"].get("max_drawdown"), candidate["stats"].get("max_drawdown")
        if before is not None and after is not None and after <= before + 1e-8:
            reasons.append("drawdown_not_improved")
    return {
        "objective": objective,
        "decision": "rejected_topic" if reasons else "exploratory_followup_only",
        "reasons": reasons,
        "production_promotion": False,
        "evidence_limit": "历史开发样本及PIT缺口，缺冻结后前瞻观察；本结果不能验证胜率提升",
    }


def report(args):
    output, report_dir = args.output.resolve(), args.report.resolve()
    identity = json.loads((output / "identity.json").read_text())
    rows, cards = [], []
    for _, members in BUILTIN_FAMILIES.values():
        for sid in members:
            card = {"strategy_id": sid, "identity_sha256": identity["sha256"],
                    "pending_topic": PENDING_TOPICS.get(sid), "trials": []}
            base_path = output / "results" / f"{sid}__original.json"
            if not base_path.exists():
                card["status"] = "not_run"
                cards.append(card)
                continue
            baseline = json.loads(base_path.read_text())
            for path in sorted((output / "results").glob(f"{sid}__*.json")):
                result = json.loads(path.read_text())
                if result["identity_sha256"] != identity["sha256"]:
                    raise ValueError("结果身份与批次不匹配")
                variant = path.stem.split("__")[1]
                decision = {"decision": "baseline_retained", "production_promotion": False}
                if variant != "original":
                    decision = candidate_decision(
                        baseline, result, objective="risk_return" if variant.startswith("risk_") else "win_rate",
                    )
                    decision["monthly_block_comparison"] = monthly_block_comparison(
                        baseline["trades"], result["trades"], draws=2000, seed=20261019,
                    )
                    decision["trade_set_change"] = _differences(baseline, result)
                card["trials"].append({
                    "variant": variant, "result_sha256": sha256(path),
                    "definition_sha256": result["definition_sha256"],
                    "params": result["resolved_params"], "overrides": result["config"].get("overrides"),
                    "metrics": result["metrics"],
                    "stats": {k: result["stats"].get(k) for k in (
                        "total_return", "max_drawdown", "avg_exposure", "unclosed_positions",
                        "pending_exit_positions", "total_fees",
                    )}, **decision,
                })
                rows.append({
                    "strategy_id": sid, "variant": variant, "decision": decision["decision"],
                    "trades": result["metrics"]["trades"],
                    **{key: result["metrics"].get(key) for key in ("win_rate", "expectancy", "profit_factor", "payoff_ratio")},
                    **{key: result["stats"].get(key) for key in ("total_return", "max_drawdown", "avg_exposure")},
                })
            expected = len(identity["identity"]["configs"].get(sid, []))
            card["status"] = "exploratory_complete" if len(card["trials"]) == expected else "partial"
            cards.append(card)
    write_json(report_dir / "decision-cards.json", cards)
    if rows:
        pl.from_dicts(rows, infer_schema_length=None).write_csv(report_dir / "fair-metrics.csv")
    write_json(report_dir / "trial-ledger.json", {
        "identity_sha256": identity["sha256"], "previous_trials": identity["identity"]["previous_trials"],
        "registered_trials": identity["identity"]["configs"], "rows": rows,
        "multiple_testing": "月块区间未校正跨轮多重尝试，仅诊断，不作为晋级依据",
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--data", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--start", default="2021-10-18")
    execute.add_argument("--end", default="2026-10-16")
    execute.add_argument("--slippage-bps", type=float, default=5.)
    execute.add_argument("--max-symbols", type=int, default=0)
    execute.add_argument("--strategies", nargs="+")
    summarize = commands.add_parser("report")
    summarize.add_argument("--output", type=Path, required=True)
    summarize.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    (run if args.command == "run" else report)(args)


if __name__ == "__main__":
    main()
