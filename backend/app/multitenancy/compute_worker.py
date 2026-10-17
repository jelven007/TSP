"""Credential-free full-engine entrypoint, executed only inside the sandbox."""
# ruff: noqa: RUF001 -- Chinese compute messages use localized punctuation.
from __future__ import annotations

import base64
import contextlib
import json
import math
import re
import sys
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path

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
    PROTOCOL.write(json.dumps(
        safe_value({"type": kind, **values}), ensure_ascii=False, allow_nan=False, default=str,
    ) + "\n")
    PROTOCOL.flush()


def execute(payload: dict, private_root: Path) -> dict:
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.backtest.worker import (
        _decode_backtest_config,
        _decode_optimize_config,
        _decode_walkforward_config,
    )
    from app.config import settings
    from app.multitenancy.strategies import definition_payload
    from app.strategy.engine import StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository

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
    engine = StrategyEngine(directories, override_loader=lambda sid: payload.get("overrides", {}).get(sid, {}))
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
    store = DataStore(Path("/market"), read_only=True)
    try:
        repo = KlineRepository(store)
        if kind == "monitor":
            from app.multitenancy.monitor_compute import evaluate_snapshot

            return evaluate_snapshot(payload, repo, engine, private_root)
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

            as_of = date.fromisoformat(payload["as_of"])
            svc = ScreenerService(repo, asset_type=payload.get("asset_type", "stock"))
            ids = payload["strategy_ids"]
            for sid in ids:
                if engine.get(sid).meta.get("research_only"):
                    raise ValueError(f"unknown strategy: {sid}")
            context = svc.build_strategy_context(
                engine, as_of, ids, timeframe=payload.get("timeframe", "1d"),
                params_map=payload.get("params_map"), overrides_map=payload.get("overrides_map"),
            )
            if context.current is not None and context.current.is_empty():
                raise ValueError("无可用行情数据，请稍后重试")
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

        service = StrategyBacktestService(backtest_engine, engine)
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
    finally:
        store.db.close()


def main():
    raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("compute input exceeds limit")
    payload = json.loads(raw)
    # No global private caches, disk writes, or unmanaged generation creation.
    from app.config import settings
    from app.factors.registry import private_factor_registry
    from app.strategy.custom_signals import private_signal_registry

    settings.backtest_matrix_disk_cache_enabled = False
    with (tempfile.TemporaryDirectory(prefix="tsp-job-") as directory,
          private_factor_registry(payload.get("factors", []), strict=True),
          private_signal_registry(payload.get("signals", [])),
          contextlib.redirect_stdout(sys.stderr)):
        try:
            result = execute(payload, Path(directory))
            emit("result", payload=result)
        except Exception as exc:
            emit("error", status=400, message=str(exc))


if __name__ == "__main__":
    main()
