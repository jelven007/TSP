"""Opt-in execution for 173004552, with the mature engine as its fallback."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time

import polars as pl

from app.market_time import as_cn_datetime, cn_today
from app.strategy import s02_173004552 as routed
from app.strategy.engine import StrategyEngine, StrategyResult, _sanitize
from app.strategy.s01_signals import stored_definitions
from app.strategy.signal_price_suggestions import suggested_lots


class RoutedStrategyEngine(StrategyEngine):
    def run(self, strategy_id, context, pool=None, params=None, overrides=None):
        if strategy_id != routed.SID:
            return super().run(strategy_id, context, pool, params, overrides)
        strategy = self.get(strategy_id)
        if not routed.enabled(strategy):
            return self._run_common_s05(strategy, context, pool, params, overrides)
        self.validate_context(strategy, context)
        day = context.as_of
        if day > cn_today():
            raise ValueError("未来交易日只可读取已保存的选名单")
        stored = (context.selection_snapshots or {}).get(strategy_id)
        loader = getattr(self, "_selection_loader", None)
        if stored is None and callable(loader):
            stored = loader(strategy_id, day)
        result = StrategyResult(day, strategy_id, [], 0)
        result.section_mode = "signals"
        result.sections = {"sell": [], "select": [], "buy": []}
        result.section_statuses = {"sell": "success", "select": "success", "buy": "success"}
        result.section_errors = {}

        def incomplete(message, state="waiting"):
            result.entry_filter_error = message
            result.section_errors["buy"] = message
            result.section_statuses["buy"] = state
            return result

        try:
            branch = routed.frozen_route(stored, day)
        except ValueError as exc:
            result.section_statuses["select"] = "waiting"
            result.section_errors["select"] = str(exc)
            return incomplete(str(exc))
        result.selection_snapshot = stored
        result.sections["select"] = _sanitize(stored["rows"])
        if context.realtime and day == cn_today() and (stored.get("entry_route") or {}).get("schema") != routed.MARKET_ROUTE:
            return incomplete("等待目标交易日S05整数分支参数归档")
        scoped = (context.daily_pool_snapshots or {}).get(strategy_id)
        pool_loader = getattr(self, "_daily_pool_loader", None)
        if scoped is None and callable(pool_loader):
            scoped = pool_loader(strategy_id, day)
        if scoped is not None:
            from app.strategy.daily_pool import DEFINITION_VERSION
            if (scoped.get("strategy_id") != strategy_id or scoped.get("as_of") != str(day)
                    or scoped.get("definition_version") != DEFINITION_VERSION
                    or not isinstance(scoped.get("symbols"), list)
                    or scoped.get("total") != len(set(scoped["symbols"]))):
                return incomplete("当日完整股票池的身份不匹配", "failed")
            result.daily_stock_pool = scoped
        if branch is None:
            result.section_statuses["buy"] = "rejected"
            result.monitor_tasks = {"buy": routed.no_market_route_receipt(stored)}
            return result
        chosen = self._effective_signals(overrides or {}, "entry_signals", strategy.entry_signals)
        column = next((name for name in chosen
                       if name == "csgi_" + routed.ENTRY_IDS[branch]), None)
        if column is None:
            return result
        signal_id = column.removeprefix("csgi_")
        observed = context.observed_at or context.completed_through
        deadline = datetime.combine(day, time(14, 55))
        if context.realtime:
            result.entry_deadline_at = deadline.isoformat()
            if observed is not None and as_cn_datetime(observed).replace(tzinfo=None) > deadline:
                return incomplete("超过14:55:00最晚实时入场确认时间", "expired")
        try:
            definition = next((s for s in stored_definitions(self) if s["id"] == signal_id), None)
            if definition is None or definition.get("enabled") is not True:
                raise ValueError("当日分支未配置启用的入场点")
            if context.input_errors:
                return incomplete("; ".join(context.input_errors), "failed")
            allowed = set(scoped["symbols"]) if scoped else None
            # A caller-specified display scope cannot change the branch's head candidate.
            effective = {**stored, "rows": [row for row in stored["rows"]
                         if allowed is None or row["symbol"] in allowed]}
            frame = context.history if context.history is not None else pl.DataFrame()
            hits, evidence = routed.evaluate_signal(
                frame, effective, day, definition, observed, unavailable=context.unavailable_input_symbols)
        except (ValueError, pl.exceptions.PolarsError) as exc:
            return incomplete(str(exc), "failed")
        result.entry_data_quality = {column: evidence}
        if evidence["stage"] == "waiting":
            return incomplete(f"等待{evidence['confirmed_at'][11:]}确认入场触发器")
        rows = _sanitize([{**row, "suggested_lots_100k": suggested_lots(row["close"])}
                          for row in hits if pool is None or row["symbol"] in pool])
        result.sections["buy"] = result.rows = rows
        result.total = len(rows)
        result.scores = {row["symbol"]: row["score"] for row in rows}
        result.entry_signal_hits = [
            {"symbol": row["symbol"], "signals": [column], "evaluated_at": row["entry_evaluated_at"]}
            for row in rows]
        return result

    def _run_common_s05(self, strategy, context, pool, params, overrides):
        """Scope the ordinary scan before any common-template signal can run."""
        sid = routed.SID
        snapshots = context.selection_snapshots or {}
        selection = snapshots.get(sid)
        loader = getattr(self, "_selection_loader", None)
        if selection is None and callable(loader):
            selection = loader(sid, context.as_of)
        if selection is not None:
            context = replace(context, selection_snapshots={**snapshots, sid: selection})
        if ((selection or {}).get("entry_route") or {}).get("schema") != routed.MARKET_ROUTE:
            # Historical definitions without the numeric routing contract are retained.
            return super().run(sid, context, pool, params, overrides)

        error = None
        try:
            branch = routed.frozen_daily_branch(selection, context.as_of)
        except ValueError as exc:
            branch, error = None, str(exc)
        expected = "csgi_" + routed.ENTRY_IDS[branch] if branch else None
        requested = self._effective_signals(overrides or {}, "entry_signals", strategy.entry_signals)
        columns = [column for column in requested if column == expected]
        result = super().run(sid, context, pool, params, {**(overrides or {}), "entry_signals": columns})
        if columns or result.section_mode != "signals":
            return result
        result.sections["buy"], result.rows, result.total = [], [], 0
        result.scores, result.entry_signal_hits = {}, []
        result.section_statuses = dict(result.section_statuses or {})
        result.section_errors = dict(result.section_errors or {})
        if error or branch:
            message = error or "当日分支未配置启用的入场信号"
            result.entry_filter_error = message
            result.section_statuses["buy"], result.section_errors["buy"] = "waiting", message
        else:
            result.entry_filter_error = None
            result.section_errors.pop("buy", None)
            result.section_statuses["buy"] = "rejected"
            result.monitor_tasks = {**(result.monitor_tasks or {}), "buy": routed.no_market_route_receipt(selection)}
        return result
