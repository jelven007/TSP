"""One bounded strategy subprocess; it receives no database credentials."""

import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path


def catalog() -> list[dict]:
    from app.strategy.engine import StrategyEngine

    engine = StrategyEngine([Path(__file__).parents[1] / "strategy" / "builtin"])
    result = []
    for strategy in engine.strategy_definitions():
        if strategy.meta.get("research_only") or strategy.meta.get("timeframes", ["1d"]) != ["1d"]:
            continue
        maximums = {p["id"]: p.get("max", p.get("default")) for p in strategy.meta.get("params", [])
                    if p["type"] == "int"}
        bars = engine.required_history_bars([strategy.meta["id"]], params_map={strategy.meta["id"]: maximums})
        result.append({
            "id": strategy.meta["id"],
            "code_hash": hashlib.sha256(strategy.file_path.read_bytes()).hexdigest(),
            "definition": {
                **strategy.meta, "basic_filter": strategy.basic_filter,
                "execution_backend": strategy.execution_backend,
                "history_bars": min(500, max(120, bars)),
            },
        })
    return result


def execute(payload: dict) -> dict:
    import polars as pl

    from app.strategy.engine import StrategyDataContext, StrategyEngine

    strategy = payload["strategy"]
    engine = StrategyEngine([Path(__file__).parents[1] / "strategy" / "builtin"])
    definition = engine.get(strategy["template_id"])
    if hashlib.sha256(definition.file_path.read_bytes()).hexdigest() != strategy["template_hash"]:
        raise ValueError("strategy_template_version_changed")
    as_of = date.fromisoformat(payload["as_of"])
    history = pl.DataFrame(payload["market"]["rows"])
    if "date" not in history.columns:
        raise ValueError("market_history_missing")
    history = history.with_columns(pl.col("date").cast(pl.String).str.to_date())
    history = history.filter(pl.col("date") <= as_of)
    current = history.filter(pl.col("date") == as_of)
    if current.is_empty():
        raise ValueError("market_history_missing_as_of")
    result = engine.run(
        strategy["template_id"], StrategyDataContext("stock", "1d", as_of, current, history),
        pool=payload["symbols"], overrides=strategy["overrides"],
    )
    return {
        "as_of": str(as_of), "strategy_id": strategy["id"], "strategy_version": strategy["version"],
        "template_hash": strategy["template_hash"], "release_id": payload["market"]["release_id"],
        "total": result.total, "rows": result.rows, "elapsed_ms": result.elapsed_ms,
    }


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "catalog":
        print(json.dumps(catalog(), default=str, allow_nan=False))
        return
    payload = json.load(sys.stdin)
    if sys.platform != "win32":
        import resource

        cpu = int(os.environ.get("TSP_COMPUTE_CPU_SECONDS", "60"))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        if sys.platform == "linux":
            # LLVM and Polars reserve virtual mappings well beyond their resident memory.
            # The Worker cgroup bounds real memory; this separately bounds address space.
            resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
    print(json.dumps(execute(payload), default=str, allow_nan=False))


if __name__ == "__main__":
    main()
