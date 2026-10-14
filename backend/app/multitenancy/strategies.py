"""Full strategy catalog with SQL source ownership and remote code execution."""
# ruff: noqa: RUF001 -- Chinese strategy messages use localized punctuation.
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.executor import ComputeExecutor
from app.strategy.engine import (
    CompositeChild,
    CompositeSpec,
    StrategyDataContext,
    StrategyDef,
    StrategyEngine,
    StrategyResult,
)

DEFINITION_FIELDS = (
    "meta", "basic_filter", "entry_signals", "exit_signals", "stop_loss",
    "trailing_stop", "trailing_take_profit_activate", "trailing_take_profit_drawdown",
    "max_hold_days", "lookback_days", "source", "execution_backend", "minute_daily_bars",
)


def definition_payload(strategy: StrategyDef) -> dict:
    return {**{key: getattr(strategy, key) for key in DEFINITION_FIELDS},
            "required_features": sorted(strategy.required_features),
            "children": ([{"strategy_id": child.strategy_id, "weight": child.weight}
                          for child in strategy.composite.children] if strategy.composite else None)}


def metadata_definition(value: dict) -> StrategyDef:
    children = value.get("children")
    return StrategyDef(
        **{key: value[key] for key in DEFINITION_FIELDS},
        required_features=frozenset(value.get("required_features", [])),
        composite=CompositeSpec(tuple(CompositeChild(**child) for child in children)) if children else None,
        filter_fn=None, filter_history_fn=None,
    )


class TenantStrategyEngine(StrategyEngine):
    # ScreenerService lets the container load its own public market window,
    # avoiding private signal/factor columns in shared repository caches.
    remote_context = True

    def __init__(self, documents: TenantDocuments, builtins: StrategyEngine, executor: ComputeExecutor):
        self.documents = documents
        self.builtins = builtins
        self.executor = executor
        super().__init__([], override_loader=lambda sid: documents.get("strategy_overrides", sid, {}))
        self.reload()

    def reload(self) -> None:
        self._strategies = {sid: replace(strategy, meta=copy.deepcopy(strategy.meta))
                            for sid, strategy in self.builtins._strategies.items()}
        self._load_errors = []
        for row in self.documents.records("strategy_sources"):
            value = row["value"]
            if row["id"] in self._strategies:
                raise ValueError("private strategy cannot replace a builtin")
            self._strategies[row["id"]] = metadata_definition(value["definition"])

    def task(self, kind: str, **values) -> dict:
        return {
            "kind": kind, "tenant_id": self.documents.context.tenant_id,
            "sources": self.documents.values("strategy_sources"),
            "overrides": {row["id"]: row["value"] for row in self.documents.records("strategy_overrides")},
            "factors": self.documents.values("custom_factors"),
            "signals": self.documents.values("custom_signals"),
            **({"public_release_id": self.public_release_id} if getattr(self, "public_release_id", None) else {}),
            **values,
        }

    def save_source(
        self, sid: str, code: str, source: str, *, mode: str = "create", cancel=None, verify_access=None,
    ) -> dict:
        if source not in {"ai", "custom", "composite"}:
            raise ValueError("invalid private strategy source")
        if self.builtins.has(sid):
            raise ValueError("内置策略不可覆盖")
        records = self.documents.records("strategy_sources")
        record = next((row for row in records if row["id"] == sid), None)
        if mode == "create" and record:
            raise ValueError(f"策略 {sid} 已存在，请使用修改模式")
        if mode == "update" and not record:
            raise ValueError(f"策略 {sid} 不存在")
        sources = [row["value"] for row in records if row["id"] != sid]
        sources.append({"id": sid, "source": source, "code": code})
        response = self.executor.execute(self.task("describe", sources=sources, strategy_id=sid),
                                         **({"cancel": cancel} if cancel is not None else {}))
        definition = response["definition"]
        if definition["meta"]["id"] != sid or definition["source"] != source:
            raise ValueError("策略声明与保存目标不一致")
        from app.api.strategy import _missing_custom_signals

        missing = _missing_custom_signals(self.documents, definition["required_features"])
        if missing:
            raise ValueError("策略引用了未定义的自定义信号: " + ", ".join(missing))
        if verify_access is not None:
            verify_access()
        if cancel is not None and cancel.is_set():
            raise AccessError(409, "策略生成已取消")
        value = {"id": sid, "source": source, "code": code, "definition": definition}
        self.documents.put("strategy_sources", sid, value,
                           expected_revision=record["revision"] if record else 0,
                           expected_collection={row["id"]: row["revision"] for row in records})
        self.documents.delete("strategy_cache", "default")
        self.reload()
        return {"ok": True, "strategy_id": sid, "source": source, "path": f"{source}/{sid}.py",
                "meta": definition["meta"], "research_only": definition["meta"].get("research_only", False)}

    def source_code(self, sid: str) -> str:
        strategy = self.get(sid)
        if strategy.source == "builtin":
            return strategy.file_path.read_text(encoding="utf-8")
        value = self.documents.get("strategy_sources", sid)
        if not value:
            raise ValueError("策略源文件不存在")
        return value["code"]

    def delete_source(self, sid: str) -> None:
        strategy = self.get(sid)
        if strategy.source == "builtin":
            raise AccessError(403, "内置策略不可删除")
        records = self.documents.records("strategy_sources")
        dependents = [row["id"] for row in records
                      if any(child["strategy_id"] == sid
                             for child in row["value"]["definition"].get("children") or [])]
        if dependents:
            raise AccessError(409, f"该策略被叠加策略 {dependents} 引用，请先解除引用")
        self.documents.delete("strategy_sources", sid,
                              expected_collection={row["id"]: row["revision"] for row in records})
        self.documents.delete("strategy_overrides", sid)
        self.documents.delete("strategy_cache", "default")
        self.reload()

    def required_history_bars(self, strategy_ids: list[str], *, params_map=None, overrides_map=None) -> int:
        if all(self.builtins.has(sid) for sid in strategy_ids):
            return self.builtins.required_history_bars(
                strategy_ids, params_map=params_map, overrides_map=overrides_map,
            )
        return int(self.executor.execute(self.task(
            "plan", strategy_ids=strategy_ids, params_map=params_map or {}, overrides_map=overrides_map or {},
        ))["history_bars"])

    def run(
        self, strategy_id: str, context: StrategyDataContext, pool=None, params=None, overrides=None,
    ) -> StrategyResult:
        self.validate_context(self.get(strategy_id), context)
        response = self.executor.execute(self.task(
            "strategy", strategy_ids=[strategy_id], asset_type=context.asset_type, timeframe=context.timeframe,
            as_of=str(context.as_of), pool=pool, params_map={strategy_id: params or {}},
            overrides_map={strategy_id: overrides or {}},
        ))
        return StrategyResult(**{**response["results"][strategy_id], "as_of": context.as_of})

    def run_all(
        self, context: StrategyDataContext, *, params_map=None, overrides_map=None,
        strategy_ids=None, parallel=True,
    ) -> dict[str, StrategyResult]:
        ids = strategy_ids or [meta["id"] for meta in self.list_strategies()
                               if context.asset_type in meta.get("asset_types", ["stock"])
                               and context.timeframe in meta.get("timeframes", ["1d"])]
        for sid in ids:
            self.validate_context(self.get(sid), context)
        response = self.executor.execute(self.task(
            "strategy", strategy_ids=ids, asset_type=context.asset_type, timeframe=context.timeframe,
            as_of=str(context.as_of), params_map=params_map or {}, overrides_map=overrides_map or {},
        ))
        return {sid: StrategyResult(**{**value, "as_of": date.fromisoformat(value["as_of"])})
                for sid, value in response["results"].items()}
