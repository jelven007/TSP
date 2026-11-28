"""计算沙箱入口: 从冻结请求建立临时私有定义, 只读公共行情并返回计算回执。

协议写stdout, 策略输出及日志转stderr; 不持有租户业务库写入凭据。
"""
# ruff: noqa: RUF001 -- Chinese compute messages use localized punctuation.
from __future__ import annotations

import base64
import contextlib
import json
import math
import re
import sys
import tempfile
from dataclasses import asdict, replace
from datetime import date, datetime, time
from pathlib import Path
from time import perf_counter

from app.multitenancy.executor import MAX_MESSAGE_BYTES

PROTOCOL = sys.stdout


def safe_value(value):
    if isinstance(value, dict):
        return {str(key): safe_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def emit(kind: str, **values):
    """发送一行协议JSON并刷新; 非有限浮点转null, 不让日志混入返回正文。"""
    PROTOCOL.write(json.dumps(
        safe_value({"type": kind, **values}), ensure_ascii=False, allow_nan=False, default=str,
    ) + "\n")
    PROTOCOL.flush()


@contextlib.contextmanager
def request_repository(payload):
    """权威输入的买/广度/汇合请求不打开全局SQL视图或独立市场IO。

    上下文加载器提供已验证的清单帧; 其他请求保持既有只读仓库路径。
    """
    from types import SimpleNamespace

    from app.services.strategy_input_binding import requires_authority

    ids = payload.get("strategy_task_ids", [])
    frozen = (
        len(ids) == 1 and requires_authority(payload, ids[0])
        and payload.get("monitor_phase") in ("stocks", "breadth", "joined")
        and (payload.get("kind") == "monitor_breadth"
             or (payload.get("kind") == "monitor" and payload.get("late_task_only")))
        and all(action["kind"] == "buy"
                for plan in payload.get("strategy_task_plans", {}).values()
                for action in plan.get("actions", [])))
    if frozen:
        # Screener只需要持有路径; 冻结上下文提供全部帧, 意外市场读取将明确失败。
        yield SimpleNamespace(store=SimpleNamespace(data_dir=Path("/market")))
        return
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(Path("/market"), read_only=True)
    try:
        yield KlineRepository(store)
    finally:
        store.db.close()


def execute(payload: dict, private_root: Path) -> dict:
    """在本任务临时目录建立私有注册表, 只读绑定的 /market 公共发布。

    源码/覆盖来自本次请求, 临时文件不回写租户; 计算结果由父 Worker
    校验后提交。S01/S02等个股请求走 monitor, 可信广度绕过本注册表入口。
    """
    from app.config import settings
    from app.multitenancy.strategies import definition_payload
    from app.strategy.routed_engine import RoutedStrategyEngine

    settings.data_dir = private_root
    for path in Path("/market").iterdir():
        if path.name not in {"research", "strategies", "user_data"}:
            (private_root / path.name).symlink_to(path, target_is_directory=path.is_dir())
    directories = [Path(__file__).resolve().parents[1] / "strategy" / "builtin"]
    for source in ("custom", "ai", "composite"):
        directory = private_root / "strategies" / source
        directory.mkdir(parents=True)
        directories.append(directory)
    overrides = private_root / "user_data" / "strategy_overrides"
    overrides.mkdir(parents=True)
    if payload.get("watchlist"):
        import polars as pl

        from app.services.watchlist import _ENTRY_SCHEMA
        pl.DataFrame(payload["watchlist"], schema=_ENTRY_SCHEMA).write_parquet(
            private_root / "user_data" / "watchlist.parquet",
        )
    for sid, value in payload.get("overrides", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            raise ValueError("invalid strategy override")
        (overrides / f"{sid}.json").write_text(json.dumps(value), encoding="utf-8")
    seen = set()
    for item in payload.get("sources", []):
        sid, source = item["id"], item["source"]
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid)
                or source not in {"custom", "ai", "composite"} or sid in seen):
            raise ValueError("invalid private strategy source")
        seen.add(sid)
        (private_root / "strategies" / source / f"{sid}.py").write_text(item["code"], encoding="utf-8")
    engine = RoutedStrategyEngine(directories, override_loader=lambda sid: payload.get("overrides", {}).get(sid, {}))
    if engine.load_errors():
        raise ValueError("; ".join(item["error"] for item in engine.load_errors()))
    kind = payload["kind"]
    if kind == "describe":
        return {"definition": definition_payload(engine.get(payload["strategy_id"]))}
    if kind == "plan":
        return {"history_bars": engine.required_history_bars(
            payload["strategy_ids"], params_map=payload.get("params_map"),
            overrides_map=payload.get("overrides_map"),
        )}
    with request_repository(payload) as repo:
        if kind == "daily_selections":
            from app.services.daily_selections import compute_selections
            from app.services.screener import ScreenerService
            return compute_selections(
                engine, ScreenerService(repo), date.fromisoformat(payload["source_date"]),
                date.fromisoformat(payload["as_of"]), payload["strategy_ids"],
                payload.get("overrides_map", {}),
                source_selection=payload.get("source_selection"),
                now=datetime.fromisoformat(payload["observed_at"]) if payload.get("observed_at") else None,
                historical_replay=payload.get("historical_replay"),
            )
        if kind == "monitor":
            from app.multitenancy.monitor_compute import evaluate_snapshot

            return evaluate_snapshot(payload, repo, engine, private_root)
        if kind == "daily_stock_pools":
            if payload.get("confirmed_status"):
                from app.services.custom_status_refresh import compute_pools
                return compute_pools(
                    engine, repo, date.fromisoformat(payload["as_of"]), payload["strategy_ids"],
                    payload.get("overrides_map", {}), payload["confirmed_status"])
            from app.multitenancy.market import decode_frame
            from app.services.screener import ScreenerService
            from app.strategy.daily_pool import build_pool

            as_of = date.fromisoformat(payload["as_of"])
            current = decode_frame(payload["current"]) if payload.get("current") else None
            premarket = payload.get("premarket", False)
            observed_at = datetime.fromisoformat(payload["observed_at"]) if payload.get("observed_at") else None
            context = ScreenerService(repo).build_daily_pool_context(
                as_of, current=current, premarket=premarket, observed_at=observed_at)
            results, errors = {}, {}
            for sid in payload["strategy_ids"]:
                try:
                    if engine.get(sid).meta.get("research_only"):
                        continue
                    results[sid] = build_pool(engine, sid, context,
                        overrides=payload.get("overrides_map", {}).get(sid), premarket=premarket)
                except Exception as exc:
                    errors[sid] = str(exc)
            return {"pools": results, "errors": errors}
        from app.backtest.engine import BacktestEngine

        backtest_engine = BacktestEngine(repo)
        if kind in {"signal_backtest", "factor_backtest", "factor_batch", "auto_screen"}:
            values = dict(payload["config"])
            for key in ("start", "end"):
                if values.get(key):
                    values[key] = date.fromisoformat(values[key])
            if kind == "auto_screen":
                from app.services.auto_mining import screen_all_factors

                return screen_all_factors(backtest_engine, **values)
            if kind == "signal_backtest":
                from app.services.backtest import BacktestConfig, BacktestService

                return asdict(
                    BacktestService(repo, persist_result=False).run(
                        BacktestConfig(**values)
                    )
                )
            from app.backtest.factor import FactorBacktestService, FactorBatchConfig, FactorConfig

            factors = FactorBacktestService(backtest_engine)
            return asdict(factors.run_batch(FactorBatchConfig(**values)) if kind == "factor_batch"
                          else factors.run(FactorConfig(**values)))
        if kind == "strategy":
            from app.services.screener import ScreenerService
            from app.services.selection_clock import S05_ID

            as_of = date.fromisoformat(payload["as_of"])
            svc = ScreenerService(repo, asset_type=payload.get("asset_type", "stock"))
            ids = payload["strategy_ids"]
            for sid in ids:
                if engine.get(sid).meta.get("research_only"):
                    raise ValueError(f"unknown strategy: {sid}")
            context = svc.build_strategy_context(
                engine, as_of, ids, timeframe=payload.get("timeframe", "1d"),
                params_map=payload.get("params_map"), overrides_map=payload.get("overrides_map"),
                selection_snapshots=payload.get("selection_snapshots", {}),
            )
            context.previous_states = payload.get("previous_states")
            context.selection_snapshots = payload.get("selection_snapshots", {})
            context.daily_pool_snapshots = payload.get("daily_pool_snapshots", {})
            from app.market_time import cn_now, cn_today
            if as_of == cn_today():
                context = replace(context, observed_at=cn_now(), realtime=True,
                                  signal_task_states=payload.get("signal_task_states", {}))
            if payload.get("historical_replay") is not None:
                from app.services.s05_task_replay import historical_context, replay_strategy
                context = historical_context(context, payload["historical_replay"], ids)
                return replay_strategy(engine, svc, context, payload)
            positions = payload.get("positions", {})
            if (context.current is not None and context.current.is_empty() and not positions
                    and not (ids == [S05_ID] and context.realtime)):
                raise ValueError("无可用行情数据，请稍后重试")
            if positions:
                from app.market_time import cn_now, cn_today

                # Historical scans observe the selected day's close; live scans
                # retain seconds for the holding deadline and fresh quote check.
                observed_at = (context.observed_at if payload.get("historical_replay") is not None else
                               cn_now() if as_of == cn_today() else datetime.combine(as_of, time(15)))
                results = {}
                for sid in ids:
                    scoped = replace(context, positions=positions.get(sid), observed_at=observed_at)
                    if sid == S05_ID and scoped.realtime and scoped.positions:
                        from app.services.strategy_monitor_tasks import sell_context

                        symbols = sorted({position["symbol"] for position in scoped.positions})
                        quotes = svc._load_enriched_for_date(as_of)
                        if "symbol" in quotes.columns:
                            quotes = quotes.filter(pl.col("symbol").is_in(symbols))
                        scoped.holding_context = sell_context(
                            svc, as_of, quotes, {"rows": scoped.positions}, observed_at)
                    results[sid] = asdict(engine.run(
                        sid, scoped, pool=payload.get("pool") if len(ids) == 1 else None,
                        params=payload.get("params_map", {}).get(sid),
                        overrides=payload.get("overrides_map", {}).get(sid),
                    ))
                return {"results": results}
            if len(ids) == 1:
                sid = ids[0]
                result = engine.run(sid, context, pool=payload.get("pool"),
                                    params=payload.get("params_map", {}).get(sid),
                                    overrides=payload.get("overrides_map", {}).get(sid))
                return {"results": {sid: asdict(result)}}
            return {"results": {sid: asdict(result) for sid, result in engine.run_all(
                context, params_map=payload.get("params_map"), overrides_map=payload.get("overrides_map"),
                strategy_ids=ids, parallel=False,
            ).items()}}

        from app.backtest.strategy import StrategyBacktestService
        from app.backtest.worker import (
            _decode_backtest_config,
            _decode_optimize_config,
            _decode_walkforward_config,
        )

        service = StrategyBacktestService(
            backtest_engine, engine, s05_selection_archive=payload.get("s05_selection_archive"))
        def progress(message):
            emit("progress", payload=message)

        if kind == "backtest":
            return asdict(service.run(_decode_backtest_config(payload["config"]), progress))
        from app.backtest.optimizer import StrategyOptimizer

        optimizer = StrategyOptimizer(service, engine)
        if kind == "optimize":
            return optimizer.optimize(_decode_optimize_config(payload["config"]), progress)
        if kind == "walkforward":
            from app.backtest.walkforward import WalkForwardService

            return WalkForwardService(optimizer, service, engine).run(
                _decode_walkforward_config(payload["config"]), progress,
            )
        if kind == "mining":
            from app.backtest.mining_runtime import run_mining_runtime
            from app.services.mining_jobs import MiningRunStore

            # Runtime keeps its original directory contracts. Public files are
            # read-only links, while manifests/Parquet/overrides stay in /tmp.
            overrides = private_root / "user_data" / "strategy_overrides"
            overrides.mkdir(parents=True, exist_ok=True)
            for sid, value in payload.get("overrides", {}).items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
                    raise ValueError("invalid strategy override")
                (overrides / f"{sid}.json").write_text(json.dumps(value), encoding="utf-8")
            config = payload["config"]
            runs = MiningRunStore(private_root)
            runs.create(config["request"], config["data_fingerprint"],
                        run_id=config["run_id"], source=config.get("source", "manual"))

            result = run_mining_runtime(
                config, data_dir=private_root, service=service,
                strategy_engine=engine, progress_cb=progress,
            )
            result["artifact_data"] = {
                name: base64.b64encode(runs.artifact_path(config["run_id"], name).read_bytes()).decode("ascii")
                for name in result["artifacts"]
            }
            return result
        raise ValueError("unsupported compute operation")
def execute_request(payload: dict, private_root: Path) -> dict:
    """按类型派发; monitor_breadth 独立打开只读 /market, 不建立租户注册表。"""
    if payload.get("kind") == "health":
        return {"ready": True}
    if payload.get("kind") == "monitor_breadth":
        from app.multitenancy.monitor_compute import evaluate_breadth_phase
        with request_repository(payload) as repo:
            result = evaluate_breadth_phase(payload, repo)
        if payload.get("s02_inputs_required"):
            from app.services.strategy_input_binding import execution_environment
            result["_compute_runtime"] = execution_environment()
        return result
    if payload.get("kind") not in {"backtest", "optimize", "walkforward"}:
        result = execute(payload, private_root)
        if payload.get("s02_inputs_required"):
            from app.services.strategy_input_binding import execution_environment
            result["_compute_runtime"] = execution_environment()
        return result
    from app.services.research_snapshot import _runtime_identity

    runtime = _runtime_identity()
    if payload.get("expected_runtime") is not None and payload["expected_runtime"] != runtime:
        raise ValueError("原输入复测的运行构建已改变，请使用原构建或建立新试验")
    result = execute(payload, private_root)
    result["research"] = {**result.get("research", {}), "runtime": runtime}
    return result


def main():
    started = perf_counter()
    timing = dict.fromkeys(("input_ms", "imports_ms", "registry_ms", "execute_ms"), 0.0)
    raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("compute input exceeds limit")
    payload = json.loads(raw)
    if payload.get("kind") == "monitor_breadth":
        # 可信完整池观测不依赖租户注册表; 源码、信号或因子加载失败不能阻断广度。
        payload.update(sources=[], signals=[], factors=[])
    timing["input_ms"] = (perf_counter() - started) * 1000
    if payload.get("kind") == "health":
        execution_started = perf_counter()
        result = execute_request(payload, Path("/tmp"))
        timing["execute_ms"] = (perf_counter() - execution_started) * 1000
        timing["total_ms"] = (perf_counter() - started) * 1000
        emit("result", payload=result, worker_timing=timing)
        return
    # 私有注册表和文件只存本任务临时目录; 禁用全局矩阵磁盘缓存,
    # 不向只读公共行情创建缓存或非受控数据世代。
    imports_started = perf_counter()
    from app.config import settings
    from app.factors.registry import private_factor_registry
    from app.strategy.custom_signals import private_signal_registry

    timing["imports_ms"] = (perf_counter() - imports_started) * 1000
    settings.backtest_matrix_disk_cache_enabled = False
    registry_started = perf_counter()
    with (tempfile.TemporaryDirectory(prefix="tsp-job-") as directory,
          private_factor_registry(payload.get("factors", []), strict=True),
          private_signal_registry(payload.get("signals", [])),
          contextlib.redirect_stdout(sys.stderr)):
        timing["registry_ms"] = (perf_counter() - registry_started) * 1000
        execution_started = perf_counter()
        try:
            result = execute_request(payload, Path(directory))
            message = {"type": "result", "payload": result}
        except Exception as exc:
            message = {"type": "error", "status": 400, "message": str(exc)}
        timing["execute_ms"] = (perf_counter() - execution_started) * 1000
        timing["total_ms"] = (perf_counter() - started) * 1000
        emit(message.pop("type"), **message, worker_timing=timing)


if __name__ == "__main__":
    main()
