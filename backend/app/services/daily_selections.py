"""盘后冻结目标日资格; 正常空预选持久化, 缺依赖/失败不伪装为空预选。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as clock_time

import polars as pl

from app.cell.context import AccessError
from app.market_time import as_cn_datetime, cn_now, published_trading_days
from app.services.selection_clock import (
    S03_ID,
    S05_ID,
    SELECTION_TIME,
    SELECTION_TIME_TEXT,
    job_key,
    selection_group,
    window_for,
)
from app.services.strategy_results import ResultIdentity, StrategyResults, _encode

DEFINITION_VERSION = "postclose_selection_v1"
_scheduled_lock = threading.Lock()


def signal_strategy(strategy):
    """识别可生成盘后资格的策略能力, 不检查监控启用状态或已有归档。"""
    from app.strategy.signal_sections import supports_signal_sections

    return (getattr(strategy, "source", None) in ("custom", "ai")
            or supports_signal_sections(strategy))


def due_window(now=None, *, monitored_only=False, strategy_id=None):
    """监控按策略自己窗口恢复, S03为22~23点; 非监控恢复不跨目标日开盘。"""
    now = as_cn_datetime(now or cn_now())
    if monitored_only:
        from app.market_time import published_trading_day
        from app.strategy.overnight_runtime import next_market_day

        start, end = window_for(strategy_id)
        if (published_trading_day(now.date()) is not True
                or not start <= now.time().replace(tzinfo=None) <= end):
            return None
        return now.date(), next_market_day(now.date())
    days = sorted(day for year in (now.year - 1, now.year, now.year + 1)
                  for day in (published_trading_days(year) or []))
    sources = [day for day in days if day < now.date()
               or (day == now.date() and now.time().replace(tzinfo=None) >= SELECTION_TIME)]
    if not sources:
        return None
    source = sources[-1]
    target = next((day for day in days if day > source), None)
    if target is None or (now.date() >= target
                          and now.time().replace(tzinfo=None) >= clock_time(9, 30)):
        return None
    return source, target


def selection_identity(strategy_id, *, policy=""):
    """建立目标日资格专用归档槽; 与分钟买结果的运行身份槽分开。"""
    return ResultIdentity(strategy_id, "", "", "{}", "{}", "null", "stock", "1m",
                          daily_record=True, selection_policy=policy)


def valid_selection(snapshot, day, *, strategy_id=None):
    """校验精确前/目标日、列表与整体错误; S03还要求五维口径及最多30只。

    正常空归档有效, 范围非空且available_daily=0无效。这里不认证调度
    时点或依赖SHA-256, S03的源名单冻结入口另行检查。
    """
    from app.strategy.basic_filter import previous_session
    if (not isinstance(snapshot, dict) or snapshot.get("as_of") != str(day)
            or not isinstance(snapshot.get("rows"), list) or snapshot.get("errors")):
        return False
    if snapshot.get("source_date") != str(previous_session(date.fromisoformat(str(day)))):
        return False
    if strategy_id == S03_ID:
        from app.services.s03_selection import POLICY
        if snapshot.get("scoring_policy") != POLICY or len(snapshot["rows"]) > 30:
            return False
    if strategy_id == S05_ID:
        from app.services.s05_selection import POLICY, WEIGHTS
        if (snapshot.get("scoring_policy") != POLICY
                or snapshot.get("scoring_weights") != WEIGHTS or len(snapshot["rows"]) > 50):
            return False
    coverage = snapshot.get("coverage") or {}
    if not isinstance(coverage, dict):
        return False
    return not (coverage.get("scope_total") and coverage.get("available_daily") == 0)


def selection_policy(strategy):
    from app.strategy.s02_173004552 import enabled
    if enabled(strategy):
        from app.services.s05_selection import NUMERIC_POLICY
        return NUMERIC_POLICY
    if strategy.meta.get("id") == S05_ID:
        from app.services.s05_selection import COMMON_BRANCH_POLICY
        return COMMON_BRANCH_POLICY
    return None


def selection_record(strategy_id, day, load, *, policy=None):
    """S03先读五维口径槽, 再读原槽中的兼容归档; 不接受仅ID匹配的旧口径。

    专用槽只区分预选记录, 不改变策略逻辑身份; 读取不迁移或覆盖历史记录。
    """
    from app.strategy.s02_173004552 import POLICY as S02_POLICY
    from app.strategy.s02_173004552 import SID, frozen_route
    if strategy_id == S05_ID and policy != S02_POLICY:
        from app.services.s05_selection import (
            COMMON_BRANCH_POLICY,
            COMMON_POLICY,
            NUMERIC_POLICY,
            POLICY,
            ROUTED_POLICY,
        )
        slots = (COMMON_BRANCH_POLICY, COMMON_POLICY, NUMERIC_POLICY, ROUTED_POLICY, POLICY)
        for slot in slots if policy is None else (policy,):
            if slot not in slots:
                continue
            saved = load(selection_identity(strategy_id, policy=slot).record_id(str(day)))
            snapshot = (saved or {}).get("result", {}).get("selection_snapshot")
            if not valid_selection(snapshot, day, strategy_id=S05_ID):
                continue
            if slot in (COMMON_BRANCH_POLICY, NUMERIC_POLICY, ROUTED_POLICY):
                try:
                    if "entry_route" not in snapshot:
                        continue
                    if slot == COMMON_BRANCH_POLICY:
                        from app.strategy.s02_173004552 import frozen_daily_branch
                        frozen_daily_branch(snapshot, date.fromisoformat(str(day)))
                    else:
                        frozen_route(snapshot, date.fromisoformat(str(day)))
                    if slot == NUMERIC_POLICY and type(snapshot.get("branch_code")) is not int:
                        continue
                except ValueError:
                    continue
            return saved
        if policy is not None:
            return None
    if strategy_id == SID and policy in (None, S02_POLICY):
        saved = load(selection_identity(strategy_id, policy=S02_POLICY).record_id(str(day)))
        if saved:
            snapshot = saved.get("result", {}).get("selection_snapshot")
            try:
                frozen_route(snapshot, date.fromisoformat(str(day)))
                if valid_selection(snapshot, day):
                    return saved
            except ValueError:
                pass
        if policy is not None:
            return None
    if strategy_id != S03_ID:
        return load(selection_identity(strategy_id).record_id(str(day)))
    policies = [""]
    if strategy_id == S03_ID:
        from app.services.s03_selection import POLICY
        policies.insert(0, POLICY)
    for policy in policies:
        saved = load(selection_identity(strategy_id, policy=policy).record_id(str(day)))
        snapshot = (saved or {}).get("result", {}).get("selection_snapshot")
        if valid_selection(snapshot, day, strategy_id=strategy_id):
            return saved
    return None


class DailySelections:
    """通过结果存储读写冻结资格; 作业回执与资格快照分别保存。"""

    def __init__(self, storage):
        self.results = StrategyResults(storage)

    def read(self, strategy_id, day, *, policy=None):
        """读取目标日有效冻结预选, 并沿既有状态证据刷新展示, 缺归档返回 None。"""
        saved = selection_record(strategy_id, day, lambda key: self.results.detail(strategy_id, key),
                                 policy=policy)
        snapshot = (saved or {}).get("result", {}).get("selection_snapshot")
        from app.services.custom_status_refresh import selection
        return selection(self.results, strategy_id, snapshot) if valid_selection(
            snapshot, day, strategy_id=strategy_id) else None

    def save(self, identity, snapshot, *, started_at, input_generation=None):
        """校验后保存完整冻结资格和卖/选/买视图; 买页仍明确尚未计算。

        不按展示上限截断snapshot.rows; 已有兼容S03归档直接复用, 不重写。
        身份和输入世代交结果存储守卫校验; 自选同步由外层任务负责。
        """
        from app.strategy.basic_filter import previous_session
        target = date.fromisoformat(str(snapshot["as_of"]))
        if str(previous_session(target)) != str(snapshot["source_date"]):
            raise ValueError("预选源日期必须是目标日的精确前交易日")
        if not valid_selection(snapshot, target):
            raise ValueError("盘后预选证据异常, 需要修复重试")
        policy = ""
        from app.strategy.s02_173004552 import POLICY as S02_POLICY
        from app.strategy.s02_173004552 import SID, frozen_route
        if identity.strategy_id == S05_ID and snapshot.get("scoring_policy"):
            from app.services.s05_selection import (
                COMMON_BRANCH_POLICY,
                COMMON_POLICY,
                NUMERIC_POLICY,
                POLICY,
                ROUTED_POLICY,
            )
            if not valid_selection(snapshot, target, strategy_id=S05_ID):
                raise ValueError("S05归档不符合当前四维评分口径")
            from app.strategy.s02_173004552 import MARKET_ROUTE
            numeric = (snapshot.get("entry_route") or {}).get("schema") == MARKET_ROUTE
            policy = (COMMON_BRANCH_POLICY if numeric and snapshot.get("entry_execution") == "common_template_v1"
                      else COMMON_POLICY if snapshot.get("entry_execution") == "common_template_v1"
                      else NUMERIC_POLICY if numeric else ROUTED_POLICY if "entry_route" in snapshot else POLICY)
            if policy == COMMON_BRANCH_POLICY:
                from app.strategy.s02_173004552 import frozen_daily_branch
                frozen_daily_branch(snapshot, target)
            elif policy in (NUMERIC_POLICY, ROUTED_POLICY):
                frozen_route(snapshot, target)
            existing = selection_record(S05_ID, target,
                lambda key: self.results.detail(S05_ID, key), policy=policy)
            if existing is not None:
                return existing
        elif identity.strategy_id == SID and snapshot.get("entry_routing") == S02_POLICY:
            frozen_route(snapshot, target)
            existing = selection_record(SID, target, lambda key: self.results.detail(SID, key),
                                        policy=S02_POLICY)
            if existing is not None:
                return existing
            policy = S02_POLICY
        if identity.strategy_id == S03_ID and snapshot.get("scoring_policy"):
            from app.services.s03_selection import POLICY
            if not valid_selection(snapshot, target, strategy_id=S03_ID):
                raise ValueError("S03归档不符合当前五维评分口径")
            existing = selection_record(S03_ID, target, lambda key: self.results.detail(S03_ID, key))
            if existing is not None:
                return existing
            policy = POLICY
        frozen = {**snapshot, "identity": identity.fingerprint,
                  "definition_version": DEFINITION_VERSION}
        reference = frozen.pop("price_suggestion_reference", None)
        result = {
            "as_of": str(target), "strategy_id": identity.strategy_id,
            "rows": [], "total": 0, "elapsed_ms": 0, "section_mode": "signals",
            "selection_only": True, "selection_snapshot": frozen,
            "price_suggestion_reference": reference,
            "sections": {"sell": [], "select": snapshot["rows"], "buy": []},
            "section_errors": {"buy": "仅已生成盘后选名单, 目标交易日买入信号尚未计算"},
        }
        if identity.strategy_id == S05_ID and "entry_route" in frozen:
            from app.strategy.s02_173004552 import project_no_market_route
            project_no_market_route(result, frozen)
        return self.results.save(replace(identity, daily_record=True, selection_policy=policy), result,
                                 started_at=started_at, input_generation=input_generation)

    def job(self, key, value=None):
        """按调用方作业键读写调度回执; 无回执返回{}, 不作为预选成功证据。"""
        if self.results.tenant:
            storage = self.results.storage
            return (storage.get("daily_selection_jobs", key, {}) if value is None
                    else storage.put("daily_selection_jobs", key, value))
        with self.results._local(write=value is not None) as conn:
            if conn is None:
                return {}
            if value is not None:
                conn.execute("CREATE TABLE IF NOT EXISTS daily_selection_jobs(key TEXT PRIMARY KEY,value TEXT)")
                conn.execute("INSERT INTO daily_selection_jobs VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _encode(value)))
                return value
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='daily_selection_jobs'").fetchone():
                return {}
            row = conn.execute("SELECT value FROM daily_selection_jobs WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else {}


def compute_selections(engine, service, source, target, ids, overrides, *, now=None,
                       source_selection=None, historical_replay=None):
    """从完成源日计算目标日资格; 各策略保存异常/覆盖, 有效成员继续预选。

    共享预选内核应用范围与精确前日数值条件, 策略源码可补评分或ATR。
    买任务复用归档。股票级参考错误只影响对应股票, 完整池身份保持;
    有效输入经阈值筛为零与全无有效输入严格区分。
    返回selections/errors两张按稳定ID索引的映射, 不在这里持久化;
    共享上下文装配失败向上抛出, 每策略ValueError/OSError写入errors。
    """
    from app.services.daily_pool_schedule import scheduled_for
    from app.strategy.engine import _sanitize
    from app.strategy.signal_price_suggestions import price_reference
    from app.strategy.signal_sections import select_rows
    results, errors = {}, {}
    if S05_ID in ids:
        from app.services.s05_selection import compute_selection
        from app.strategy.s01_signals import stored_definitions
        from app.strategy.s02_173004552 import enabled, market_thresholds

        try:
            results[S05_ID] = compute_selection(
                service, source, target, source_selection, now or cn_now(),
                thresholds=market_thresholds(stored_definitions(engine)),
                common=engine is not None and not enabled(engine.get(S05_ID)),
                historical_replay=historical_replay)
        except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
            errors[S05_ID] = str(exc)
        ids = [sid for sid in ids if sid != S05_ID]
    if S03_ID in ids:
        from app.services.s03_selection import compute_selection
        try:
            results[S03_ID] = compute_selection(
                engine, service, source, target, overrides, source_selection, now or cn_now())
        except (ValueError, OSError) as exc:
            errors[S03_ID] = str(exc)
        ids = [sid for sid in ids if sid != S03_ID]
    if not ids:
        return {"selections": results, "errors": errors}
    context = service.build_selection_context(engine, source, target, ids, overrides_map=overrides)
    reference = (price_reference(context) if any(
        not engine.get(sid).meta.get("minute_execution") for sid in ids) else None)
    from app.instrument_suspension import pool_evidence
    from app.strategy.daily_pool import build_pool

    instruments = service.repo.get_instruments_asset(service.asset_type)
    scope_context = None
    if instruments is not None and not instruments.is_empty():
        from app.price_limits import parse_listing_date

        if "listing_date" in instruments.columns:
            instruments = instruments.filter(pl.Series([
                (listed := parse_listing_date(value)) is None or listed <= source
                for value in instruments["listing_date"]
            ]))
        universe = pool_evidence(service.repo, source, instruments,
                                 observed_at=datetime.combine(source, SELECTION_TIME))
        scope_context = replace(context, as_of=source,
                                current=instruments.with_columns(pl.lit(source).alias("date")),
                                pool_universe=universe)
    for sid in ids:
        try:
            coverage = None
            warnings = []
            invalid = dict(context.reference_errors or {})
            effective = {**overrides.get(sid, {}), "basic_filter": {
                **overrides.get(sid, {}).get("basic_filter", {}),
                "data_reference": "previous_trade_day",
            }}
            if scope_context is not None:
                scope = build_pool(engine, sid, scope_context, overrides={
                    **effective, "basic_filter": {**effective["basic_filter"],
                                                 "data_reference": "signal_day"},
                })
                scope_symbols = set(scope["symbols"])
                invalid = {symbol: reason for symbol, reason in invalid.items() if symbol in scope_symbols}
                unavailable = scope_symbols - set(context.current["symbol"])
                missing = unavailable - invalid.keys()
                coverage = {"schema": "daily_selection_coverage_v1", "source_date": str(source),
                            "scope_total": len(scope["symbols"]),
                            "available_daily": len(scope_symbols - unavailable),
                            "missing_symbols": sorted(missing), "complete": not (missing or invalid)}
                if scope_symbols and not coverage["available_daily"]:
                    # 范围非空却全无有效输入是失败; 有输入但数值过滤零命中才可成功为空。
                    raise ValueError(f"{source}该策略范围内无有效日线")
                if invalid:
                    coverage["invalid_symbols"] = invalid
                    if not coverage["available_daily"]:
                        raise ValueError(f"{source}该策略范围内隔离异常股票后无有效日线")
                if missing:
                    warnings.append(f"{source}范围内{len(missing)}只股票缺日线, "
                                    "本次预选仅计算已有有效日线; 完整股票池保留, 后台继续补数")
            if invalid:
                warnings.append(f"{source}隔离{len(invalid)}只收盘参考异常股票, 其余有效股票继续预选")
                warnings.extend(invalid[symbol] for symbol in sorted(invalid))
            rows = _sanitize(select_rows(engine, engine.get(sid), context, overrides=effective, scope_only=True))
            results[sid] = {"as_of": str(target), "source_date": str(source), "rows": rows,
                            "coverage": coverage, "warnings": warnings,
                            "generated_at": as_cn_datetime(now or cn_now()).isoformat(),
                            "scheduled_for": scheduled_for(source, SELECTION_TIME_TEXT)}
            if selection_policy(engine.get(sid)):
                from app.services.s02_173004552_selection import compute_selection
                results[sid] = compute_selection(service.repo, results[sid], source, target,
                                                 as_cn_datetime(now or cn_now()))
            if not engine.get(sid).meta.get("minute_execution"):
                results[sid]["price_suggestion_reference"] = reference
        except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
            results.pop(sid, None)
            errors[sid] = str(exc)
    return {"selections": results, "errors": errors}


def generate_scheduled_local(engine, service, storage, *, now=None, repair=None):
    """执行本地盘后状态刷新和各时窗预选组, 返回实际保存ID及错误回执。

    自动任务只处理已启用监控且尚无有效归档的策略; 不主动补建历史资格。
    """
    from app.services.custom_status_refresh import generate_evening_local
    try:
        refreshed = generate_evening_local(engine, service, storage, as_cn_datetime(now or cn_now()))
    except (ValueError, OSError) as exc:
        refreshed = {"saved": [], "errors": {"stock_status": str(exc)}}
        DailySelections(storage).job("stock_status_refresh", {
            "state": "failed", "errors": refreshed["errors"],
            "observed_at": as_cn_datetime(now or cn_now()).isoformat()})
    responses = [_generate_scheduled_group(engine, service, storage, now=now, repair=repair,
                                          strategy_id=sid) for sid in (None, S05_ID, S03_ID)]
    return {"saved": [sid for response in responses for sid in response["saved"]],
            "errors": {sid: error for response in responses for sid, error in response["errors"].items()},
            "status_refresh": refreshed,
            **({"watchlist_changed": True} if any(r.get("watchlist_changed") for r in responses) else {})}


def _generate_scheduled_group(engine, service, storage, *, now=None, repair=None, strategy_id=None):
    """单机锁内执行一个时窗组, 依次检查定版、计算、身份和窗口后归档。

    在途修复暂缓对应策略; 等待输入与计算异常分别记录waiting_data/failed。
    已过窗口只记过期, 不继续计算; saved仅包含本次实际保存成功的策略ID。
    S03先冻结S02依赖, 再核对settlement_ready; 已有评分但组同步失败仅重试
    同步。耗时后的定义、监控世代及真实时钟再次校验, 不能沿用开始时刻提交。
    """
    from app.services.strategy_results import capture_identities, execution_snapshot
    from app.strategy.config import list_overrides
    from app.strategy.version import digest
    now = as_cn_datetime(now or cn_now())
    from app.services.strategy_monitor_tasks import monitored_ids, monitoring_state

    attempt_start = time.monotonic()
    from app.market_time import published_trading_day
    from app.services.strategy_monitor_state import finished
    from app.services.strategy_monitor_tasks import (
        pending_selection_repairs,
        selection_receipts,
        selection_repairs,
        task_receipt,
    )
    start, end = window_for(strategy_id)
    expired = now.time().replace(tzinfo=None) > end and published_trading_day(now.date()) is True
    window = due_window(now.replace(hour=start.hour, minute=0, second=0, microsecond=0)
                        if expired else now, monitored_only=True, strategy_id=strategy_id)
    if window is None or not _scheduled_lock.acquire(blocking=False):
        return {"saved": [], "errors": {}}
    try:
        source, target = window
        store = DailySelections(storage)
        live_engine = engine
        engine = execution_snapshot(engine, storage)
        overrides = list_overrides(storage)
        active = monitored_ids(storage)
        lifecycles = {sid: monitoring_state(storage, sid) for sid in active}
        ids = [sid for sid, strategy in engine._strategies.items() if sid in active and signal_strategy(strategy)
               and not strategy.meta.get("research_only")
               and selection_group(sid) == strategy_id]
        key = job_key(target, strategy_id)
        # 有效目标日归档才证明资格完成; 单独作业成功状态不能替代归档。
        pending = [sid for sid in ids if store.read(sid, target,
                   policy=selection_policy(engine.get(sid))) is None and
                   (not expired or not finished(
                       task_receipt(storage, sid, source).get("tasks", {}).get("select", {})))]
        if expired and pending:
            job = {"source_date": str(source), "as_of": str(target), "state": "expired",
                   "errors": {sid: "盘后选股时段已结束, 尚无成功归档" for sid in pending}}
            store.job(key, job)
            selection_receipts(storage, job, now)
            return {"saved": [], "errors": job["errors"]}
        if not pending:
            if strategy_id == S03_ID and S03_ID in ids and not expired:
                from app.services.s03_watchlist import WatchlistSyncError, sync_archived_selection
                selected = store.read(S03_ID, target)
                try:
                    receipt = sync_archived_selection(storage, selected, now)
                except WatchlistSyncError as exc:
                    failed = {**store.job(key), "source_date": str(source), "as_of": str(target),
                              "state": "failed", "completed": [], "errors": {S03_ID: str(exc)},
                              "watchlist_sync": {"state": "failed", "error": str(exc)}}
                    store.job(key, failed)
                    selection_receipts(storage, failed, now)
                    return {"saved": [], "errors": failed["errors"]}
                if receipt:
                    recovered = {**store.job(key), "source_date": str(source), "as_of": str(target),
                                 "state": "succeeded", "completed": [S03_ID], "errors": {},
                                 "selections": {S03_ID: selected}, "watchlist_sync": receipt}
                    store.job(key, recovered)
                    selection_receipts(storage, recovered, now)
                    return {"saved": [], "errors": {}, "watchlist_changed": True}
            return {"saved": [], "errors": {}}
        identities = capture_identities(engine, storage, pending, overrides_map=overrides, timeframe="all")
        marker = digest({sid: identity.fingerprint for sid, identity in identities.items()})
        old = store.job(key)
        repairs = {sid: value for sid, value in (old.get("repairs") or {}).items() if sid in pending}
        blocked = pending_selection_repairs(repair, repairs) if repair else set()
        all_pending = set(pending)
        pending = [sid for sid in pending if sid not in blocked]
        if not pending:
            store.job(key, {**old, "repairs": repairs})
            return {"saved": [], "errors": old.get("errors", {})}
        blocked_errors = {sid: old.get("errors", {}).get(sid, "等待输入修复完成") for sid in blocked}
        started_at = time.time_ns()
        job = {**old, "source_date": str(source), "as_of": str(target), "signature": marker,
               "state": "running", "started_at": started_at, "attempts": old.get("attempts", 0) + 1}
        store.job(key, job)
        def repair_errors(errors):
            errors = {sid: message for sid, message in errors.items()
                      if not message.startswith("S03自选分组更新失败")}
            return {**repairs, **selection_repairs(
                repair, service.repo, "standalone", {sid: lifecycles[sid]["generation"] for sid in pending},
                job, errors, engine=engine, storage=storage, overrides=overrides)} if repair else repairs
        try:
            if strategy_id in (S03_ID, S05_ID):
                if strategy_id == S05_ID:
                    from app.services.s05_selection import freeze_source
                else:
                    from app.services.s03_selection import freeze_source
                from app.services.selection_clock import S02_ID
                dependency = job.get("source_selection") or freeze_source(store.read(S02_ID, target), source, target)
                job["source_selection"] = dependency
                store.job(key, job)
            ready = json.loads((service.repo.store.data_dir / "settlement_ready.json").read_text())
            if ready.get("day") != str(source):
                raise ValueError(f"等待{source}盘后完整数据")
            response = compute_selections(engine, service, source, target, pending, overrides, now=now,
                                          source_selection=job.get("source_selection"))
        except (OSError, ValueError) as exc:
            errors = {sid: str(exc) for sid in pending}
            failed = {**job, "state": "waiting_data", "errors": {**blocked_errors, **errors},
                      "repairs": repair_errors(errors), "finished_at": time.time_ns()}
            store.job(key, failed)
            selection_receipts(storage, failed, now)
            return {"saved": [], "errors": {sid: str(exc) for sid in pending}}
        except Exception as exc:
            errors = {sid: f"盘后预选计算失败: {exc}" for sid in pending}
            failed = {**job, "state": "failed", "errors": {**blocked_errors, **errors},
                      "repairs": repair_errors(errors), "finished_at": time.time_ns()}
            store.job(key, failed)
            selection_receipts(storage, failed, now)
            return {"saved": [], "errors": errors}
        saved, errors = [], dict(response["errors"])
        watchlist_sync = None
        for sid, selected in response["selections"].items():
            try:
                if not valid_selection(selected, target, strategy_id=sid):
                    raise ValueError("盘后预选返回不兼容归档, 需要修复重试")
                # 保存前重取当前定义、覆盖、监控世代和实际窗口, 丢弃变更前的旧计算。
                latest = capture_identities(execution_snapshot(live_engine, storage), storage, [sid],
                    overrides_map=list_overrides(storage), timeframe="all")[sid]
                if (latest.fingerprint != identities[sid].fingerprint
                        or sid not in monitored_ids(storage)
                        or monitoring_state(storage, sid) != lifecycles[sid]
                        or due_window(now + timedelta(seconds=time.monotonic() - attempt_start),
                                      monitored_only=True, strategy_id=sid) != window):
                    raise ValueError("预选配置或监控窗口已更新, 旧计算已丢弃")
                store.save(identities[sid], selected, started_at=started_at)
                if sid == S03_ID:
                    from app.services.s03_watchlist import sync_archived_selection
                    watchlist_sync = sync_archived_selection(storage, store.read(sid, target),
                        now + timedelta(seconds=time.monotonic() - attempt_start))
                saved.append(sid)
            except (AccessError, KeyError, ValueError, RuntimeError) as exc:
                errors[sid] = str(exc)
        completed = {**job, "state": "failed" if errors else "succeeded",
                           "completed": sorted(set(ids) - all_pending | set(saved)),
                           "errors": {**blocked_errors, **errors}, "repairs": repair_errors(errors),
                           "selections": response["selections"], "finished_at": time.time_ns()}
        if watchlist_sync:
            completed["watchlist_sync"] = watchlist_sync
        elif S03_ID in errors and errors[S03_ID].startswith("S03自选分组更新失败"):
            completed["watchlist_sync"] = {"state": "failed", "error": errors[S03_ID]}
        store.job(key, completed)
        selection_receipts(storage, completed, now)
        return {"saved": saved, "errors": errors,
                **({"watchlist_changed": True} if watchlist_sync else {})}
    finally:
        _scheduled_lock.release()
