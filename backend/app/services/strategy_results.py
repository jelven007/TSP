"""Durable strategy snapshots. SQL owns history; bounded memory accelerates detail reads."""
from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.cell.context import AccessError
from app.multitenancy.documents import DocumentSession, TenantDocuments, json_value
from app.strategy.eligibility import ENTRY_POLICY_VERSION
from app.strategy.version import (
    DISPLAY_FIELDS,
    digest,
    entry_trigger_policy,
    logic_version,
    runtime_identity,
)

logger = logging.getLogger(__name__)
NAMESPACE = "strategy_result_history"
LIFECYCLE_NAMESPACE = "strategy_result_lifecycle"
MEMORY_BYTES = 32 * 1024 * 1024
_memory: OrderedDict = OrderedDict()
_memory_size = 0
_memory_lock = threading.Lock()
_local_init_lock = threading.Lock()


def clear_memory() -> None:
    global _memory_size
    with _memory_lock:
        _memory.clear()
        _memory_size = 0
    from app.services.strategy_day_views import clear_memory as clear_day_views
    clear_day_views()


def _remember(key, value):
    global _memory_size
    encoded = _encode(value).encode("utf-8")
    size = len(encoded)
    with _memory_lock:
        previous = _memory.pop(key, None)
        if previous:
            _memory_size -= previous[1]
        if size > MEMORY_BYTES:
            return
        while _memory and _memory_size + size > MEMORY_BYTES:
            _, (_, old_size) = _memory.popitem(last=False)
            _memory_size -= old_size
        _memory[key] = (encoded, size)
        _memory_size += size


def _encode(value) -> str:
    def default(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError("unsupported JSON value")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=default,
                      separators=(",", ":"))


@dataclass(frozen=True)
class ResultIdentity:
    strategy_id: str
    strategy_name: str
    strategy_version: str
    params_json: str
    overrides_json: str
    pool_json: str
    asset_type: str
    timeframe: str
    runtime_identity: str = ""
    storage_generation: int = 0
    daily_record: bool = False
    selection_policy: str = ""

    @property
    def fingerprint(self) -> str:
        fields = asdict(self)
        fields.pop("storage_generation")
        fields.pop("daily_record")
        fields.pop("selection_policy")
        return digest(fields)

    def record_id(self, as_of: str) -> str:
        daily = {"strategy_daily_record": self.strategy_id}
        if self.selection_policy:
            daily["selection_policy"] = self.selection_policy
        fingerprint = digest(daily) if self.daily_record else self.fingerprint
        return f"{digest(self.strategy_id)[:16]}:{date.fromisoformat(str(as_of)).isoformat()}:{fingerprint}"

    def snapshot(self) -> dict:
        return {
            "strategy_id": self.strategy_id, "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version, "params": json.loads(self.params_json),
            "overrides": json.loads(self.overrides_json), "pool": json.loads(self.pool_json),
            "asset_type": self.asset_type, "timeframe": self.timeframe,
            "identity": self.fingerprint,
            "runtime_identity": self.runtime_identity,
        }


def execution_snapshot(engine, storage=None):
    """Keep the definition/configuration used by an in-flight run stable during reload."""
    if not hasattr(engine, "_strategies"):
        return engine
    lifecycle = StrategyResults(storage).states(engine._strategies) if storage is not None else None
    snapshot = copy.copy(engine)
    if storage is not None:
        snapshot._daily_state_loader = lambda sid, day: previous_daily_state(storage, sid, day)
        from app.market_time import cn_today
        from app.services.daily_selections import DailySelections
        snapshot._selection_loader = lambda sid, day: DailySelections(storage).read(
            sid, day, live_entry=day == cn_today())
        from functools import lru_cache

        from app.services.daily_stock_pools import DailyStockPools

        @lru_cache(maxsize=4)
        def pools(day):
            return DailyStockPools(storage).snapshots(engine._strategies, day)

        snapshot._daily_pool_loader = lambda sid, day: pools(day).get(sid)
    if lifecycle is not None:
        snapshot._result_lifecycle = lifecycle
    if hasattr(engine, "_strategies"):
        snapshot._strategies = dict(engine._strategies)
    loader = getattr(engine, "_override_loader", None)
    if getattr(engine, "remote_context", False):
        snapshot._task_snapshot = engine.task("strategy")
        overrides = snapshot._task_snapshot["overrides"]
        snapshot._override_loader = lambda sid: copy.deepcopy(overrides.get(sid, {}))
    elif callable(loader):
        overrides = {sid: loader(sid) for sid in snapshot._strategies}
        snapshot._override_loader = lambda sid: copy.deepcopy(overrides.get(sid, {}))
    return snapshot


def input_generation(repo, engine, asset_type):
    getter = getattr(repo, "get_matrix_data_generation", None)
    return {"market": getter(asset_type) if callable(getter) else None,
            "public_release_id": getattr(engine, "public_release_id", None)}


def previous_daily_state(storage, strategy_id, as_of):
    """Read only the immediately preceding market day, never a future/latest state."""
    from app.market_time import published_trading_days
    day = date.fromisoformat(str(as_of))
    past = sorted(d for year in (day.year - 1, day.year)
                  for d in published_trading_days(year) if d < day)
    if not past:
        raise ValueError("缺少前交易日历")
    identity = ResultIdentity(strategy_id, "", "", "{}", "{}", "null", "stock", "1m", daily_record=True)
    saved = StrategyResults(storage).read(identity, str(past[-1]))
    state = (saved or {}).get("result", {}).get("state")
    if not state:
        first = StrategyResults(storage).read(identity, str(day))
        state = (first or {}).get("result", {}).get("initial_state")
        saved = first
    if not state or not state.get("complete") or str(state.get("as_of")) != str(past[-1]):
        raise ValueError(f"{strategy_id}缺少{past[-1]}完整组合状态, 请先运行并归档基准回测")
    params = (saved or {}).get("summary", {}).get("params", {})
    return {**state, **{k: params[k] for k in ("suspension_policy", "suspension_events")
                      if k in params and k not in state}}


def _dependencies(storage):
    from app.factors import store as factor_store
    from app.strategy import custom_signals

    def semantic(value):
        if isinstance(value, dict):
            return {k: semantic(v) for k, v in value.items()
                    if k not in DISPLAY_FIELDS | {"_storage_revision", "created_at", "updated_at"}}
        if isinstance(value, list):
            return [semantic(v) for v in value]
        return value

    return {"factors": semantic(factor_store.load_all(storage)),
            "signals": semantic(custom_signals.load_all(storage))}


def capture_identities(engine, storage, strategy_ids, *, params_map=None, overrides_map=None,
                       asset_type="stock", timeframe="1d"):
    dependencies = _dependencies(storage)
    states = getattr(engine, "_result_lifecycle", None)
    if states is None:
        states = StrategyResults(storage).states(strategy_ids)
    return {
        sid: capture_identity(
            engine, storage, sid, params=(params_map or {}).get(sid),
            overrides=(overrides_map or {}).get(sid), asset_type=asset_type,
            timeframe=(engine.get(sid).meta.get("timeframes") or ["1d"])[0] if timeframe == "all" else timeframe,
            dependencies=dependencies,
            lifecycle=states.get(sid, {}),
        ) for sid in strategy_ids
    }


def capture_identity(engine, storage, strategy_id, *, params=None, overrides=None,
                     pool=None, asset_type="stock", timeframe="1d", dependencies=None,
                     lifecycle=None) -> ResultIdentity:
    """Capture before execution, using the loaded definition and resolved defaults."""
    strategy = engine.get(strategy_id)
    if lifecycle is None and getattr(engine, "_result_lifecycle", None) is not None:
        lifecycle = engine._result_lifecycle.get(strategy_id, {})
    lifecycle = (StrategyResults(storage).states([strategy_id]).get(strategy_id, {})
                 if lifecycle is None else lifecycle)
    if lifecycle.get("deleted"):
        raise AccessError(409, "策略已删除, 请刷新策略列表")
    from app.strategy.engine import StrategyEngine
    resolved = StrategyEngine.resolve_params(strategy, params, overrides)
    configuration = {key: value for key, value in (overrides or {}).items()
                     if key not in DISPLAY_FIELDS and key != "params"}

    def version(sid):
        definition = engine.get(sid)
        captured = getattr(definition, "logic_version", "")
        if not captured:
            # Old tenant definitions have code but predate the version field.
            source = getattr(engine, "source_code", None)
            captured = logic_version(source(sid)) if callable(source) else digest({
                "meta": {k: v for k, v in definition.meta.items() if k not in DISPLAY_FIELDS},
                "basic_filter": getattr(definition, "basic_filter", {}),
            })
        children = []
        if getattr(definition, "composite", None):
            for child in definition.composite.children:
                override_loader = getattr(engine, "_override_loader", None)
                child_override = override_loader(child.strategy_id) if callable(override_loader) else {}
                children.append({"id": child.strategy_id, "version": version(child.strategy_id),
                                 "overrides": child_override})
        return {"logic": captured, "children": children}

    # Private formula changes affect computed columns even if the strategy source stays unchanged.
    if dependencies is None:
        dependencies = _dependencies(storage)
    return ResultIdentity(
        strategy_id=strategy_id,
        strategy_name=str((overrides or {}).get("name") or strategy.meta.get("name") or strategy_id),
        strategy_version=digest({"strategy": version(strategy_id),
                                 "entry_policy": ENTRY_POLICY_VERSION, "dependencies": dependencies,
                                 **entry_trigger_policy(strategy, overrides or {})}),
        params_json=json.dumps(resolved, sort_keys=True, ensure_ascii=False, allow_nan=False),
        overrides_json=json.dumps(configuration, sort_keys=True, ensure_ascii=False, allow_nan=False),
        pool_json=json.dumps(sorted(set(pool)) if pool is not None else None),
        asset_type=asset_type, timeframe=timeframe,
        runtime_identity=runtime_identity(strategy, overrides or {}),
        storage_generation=int(lifecycle.get("generation", 0)),
        daily_record=strategy.meta.get("minute_execution") == "momentum_capacity_v1",
    )


class StrategyResults:
    def __init__(self, storage):
        self.storage = storage
        self.tenant = isinstance(storage, TenantDocuments)
        self.cacheable = not isinstance(storage, DocumentSession)
        self.path = None if self.tenant else Path(storage) / "user_data" / "strategy_results.sqlite3"
        self.scope = (("tenant", id(storage.db), storage.context.tenant_id) if self.tenant
                      else ("local", str(self.path.resolve())))

    @contextmanager
    def _local(self, *, write=False):
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                # WAL conversion precedes the transaction and does not wait for
                # another first writer even with SQLite's busy timeout.
                with _local_init_lock:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=FULL")
                    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_results(
                        id TEXT PRIMARY KEY, summary TEXT NOT NULL, value TEXT NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 1)""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_result_lifecycle(
                        strategy_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                        deleted INTEGER NOT NULL)""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS daily_stock_pools(
                        id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, day TEXT NOT NULL,
                        started_at INTEGER NOT NULL, value TEXT NOT NULL)""")
                    conn.execute("""CREATE INDEX IF NOT EXISTS daily_stock_pools_day_idx
                        ON daily_stock_pools(strategy_id,day,started_at DESC,id DESC)""")
                    from app.services.strategy_day_views import initialize
                    initialize(conn)
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            yield conn
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _state(self, conn, strategy_id):
        if conn is None:
            return {}
        if self.tenant:
            row = conn.execute(
                "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                (LIFECYCLE_NAMESPACE, strategy_id),
            ).fetchone()
            return row["value"] if row else {}
        # Existing SQLite histories predate the lifecycle table.
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_result_lifecycle'",
        ).fetchone():
            return {}
        row = conn.execute(
            "SELECT generation,deleted FROM strategy_result_lifecycle WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()
        return dict(row) if row else {}

    def states(self, strategy_ids):
        if not strategy_ids:
            return {}
        if self.tenant:
            with self.storage._transaction() as conn:
                rows = conn.execute(
                    "SELECT id,value FROM business.documents WHERE namespace=%s AND id=ANY(%s)",
                    (LIFECYCLE_NAMESPACE, list(strategy_ids)),
                ).fetchall()
            return {row["id"]: row["value"] for row in rows}
        with self._local() as conn:
            if conn is None or not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_result_lifecycle'",
            ).fetchone():
                return {}
            ids = list(strategy_ids)
            rows = conn.execute(
                f"""SELECT strategy_id,generation,deleted FROM strategy_result_lifecycle
                    WHERE strategy_id IN ({','.join('?' for _ in ids)})""", ids,
            ).fetchall()
            return {row["strategy_id"]: dict(row) for row in rows}

    def _set_state(self, conn, strategy_id, generation, deleted):
        if self.tenant:
            from psycopg.types.json import Jsonb
            conn.execute(
                """INSERT INTO business.documents(tenant_id,namespace,id,value) VALUES(%s,%s,%s,%s)
                   ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET value=excluded.value,
                   revision=business.documents.revision+1,updated_at=now()""",
                (self.storage.context.tenant_id, LIFECYCLE_NAMESPACE, strategy_id,
                 Jsonb({"generation": generation, "deleted": deleted})),
            )
        else:
            conn.execute(
                """INSERT INTO strategy_result_lifecycle VALUES(?,?,?)
                   ON CONFLICT(strategy_id) DO UPDATE SET generation=excluded.generation,deleted=excluded.deleted""",
                (strategy_id, generation, int(deleted)),
            )

    def _check_generation(self, conn, strategy_id, generation):
        state = self._state(conn, strategy_id)
        if state.get("deleted") or int(state.get("generation", 0)) != generation:
            raise AccessError(409, "策略已删除或重建, 本次旧任务结果已丢弃")

    @contextmanager
    def guard(self, strategy_id, generation):
        """Serialize a derived result write with strategy deletion."""
        transaction = self.storage._transaction if self.tenant else self._local
        with transaction(write=True) as conn:
            self._check_generation(conn, strategy_id, generation)
            yield

    def activate(self, strategy_id):
        # Only a previously deleted ID needs a durable lifecycle record.
        if not self.states([strategy_id]).get(strategy_id, {}).get("deleted"):
            return
        transaction = self.storage._transaction if self.tenant else self._local
        with transaction(write=True) as conn:
            state = self._state(conn, strategy_id)
            self._set_state(conn, strategy_id, int(state.get("generation", 0)), False)

    @contextmanager
    def deleting_strategy(self, strategy_id):
        """Delete every snapshot and fence old executions in the same transaction."""
        transaction = self.storage._transaction if self.tenant else self._local
        with transaction(write=True) as conn:
            state = self._state(conn, strategy_id)
            self._set_state(conn, strategy_id, int(state.get("generation", 0)) + 1, True)
            from app.services.strategy_day_views import remove
            remove(self, conn, strategy_id)
            if self.tenant:
                if self.storage.db.settings.schema_version >= 7:
                    conn.execute("DELETE FROM business.daily_stock_pools WHERE strategy_id=%s", (strategy_id,))
                conn.execute(
                    """DELETE FROM business.documents WHERE namespace=%s
                       AND value->'summary'->>'strategy_id'=%s""", (NAMESPACE, strategy_id),
                )
            else:
                conn.execute("DELETE FROM daily_stock_pools WHERE strategy_id=?", (strategy_id,))
                conn.execute(
                    "DELETE FROM strategy_results WHERE json_extract(summary,'$.strategy_id')=?",
                    (strategy_id,),
                )
            yield
        # Other processes recheck SQL before using their own hot caches.
        if self.cacheable:
            global _memory_size
            prefix = digest(strategy_id)[:16] + ":"
            with _memory_lock:
                for key in [key for key in _memory if key[0] == self.scope and key[1].startswith(prefix)]:
                    _, size = _memory.pop(key)
                    _memory_size -= size

    def _with_previous_buy(self, conn, identity, result, as_of):
        if result.get("section_mode") != "signals":
            return result
        from app.services.strategy_day_views import _get, _key
        is_s05 = identity.strategy_id == "custom_mvxxcs05"

        def load(record_id):
            if self.tenant:
                found = conn.execute(
                    "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                    (NAMESPACE, record_id),
                ).fetchone()
                return found["value"] if found else None
            found = conn.execute("SELECT value FROM strategy_results WHERE id=?", (record_id,)).fetchone()
            return json.loads(found["value"]) if found else None

        roster = _get(self, conn, "strategy_sell_rosters",
                      _key(identity.strategy_id, as_of, identity.asset_type, identity.timeframe))
        if is_s05 and roster and roster.get("source_record_id"):
            from app.strategy.s02_173004552 import project_daily_buys, with_entry_branches

            source = load(roster["source_record_id"])
            if source and source["summary"]["as_of"] == roster.get("source_date"):
                prior = project_daily_buys(source["result"])
                if (prior.get("section_errors") or {}).get("buy"):
                    roster = None
                else:
                    allowed = {row["symbol"] for row in (prior.get("sections") or {}).get(
                        "buy", prior.get("rows", []))}
                    rows = [row for row in roster["rows"] if row["symbol"] in allowed]
                    roster = {**roster, "rows": with_entry_branches(rows, prior.get("entry_signal_hits") or [])}
        if roster is not None:
            current = copy.deepcopy(result)
            current["sections"]["sell"] = copy.deepcopy(roster["rows"])
            current["sell_roster"] = roster
            errors = dict(current.get("section_errors") or {})
            s05_pending = is_s05 and bool(roster["rows"])
            if not s05_pending and (current.get("section_statuses") or {}).get("sell") != "failed":
                errors.pop("sell", None)
            if (current.get("section_statuses") or {}).get("sell") in (None, "waiting", "unavailable"):
                current["section_statuses"] = {**(current.get("section_statuses") or {}),
                                              "sell": "waiting" if s05_pending else "success"}
            current["section_errors"] = errors
            return current
        from app.strategy.basic_filter import previous_session
        previous = str(previous_session(date.fromisoformat(as_of)))
        prefix = digest(identity.strategy_id)[:16] + ":" + previous + ":"
        if self.tenant:
            row = conn.execute(
                """SELECT id,value FROM business.documents WHERE namespace=%s AND id LIKE %s
                   AND value->'summary'->>'strategy_id'=%s
                   AND value->'summary'->>'as_of'=%s
                   AND value->'summary'->>'asset_type'=%s
                   AND value->'summary'->>'timeframe'=%s
                   AND value->'result'->>'execution_error' IS NULL
                   AND value->'result'->>'entry_filter_error' IS NULL
                   AND value->'result'->'section_errors'->>'buy' IS NULL
                   AND COALESCE(value->'result'->>'selection_only','false')!='true'
                   AND (value->'result'->>'sections' IS NULL
                        OR value->'result'->>'section_mode'='signals')
                   ORDER BY (value->'summary'->>'started_at')::bigint DESC,id DESC LIMIT 1""",
                (NAMESPACE, prefix + "%", identity.strategy_id, previous,
                 identity.asset_type, identity.timeframe),
            ).fetchone()
            archived = row["value"] if row else None
        else:
            row = conn.execute(
                """SELECT id,value FROM strategy_results WHERE id LIKE ?
                   AND json_extract(summary,'$.strategy_id')=?
                   AND json_extract(summary,'$.as_of')=?
                   AND json_extract(summary,'$.asset_type')=?
                   AND json_extract(summary,'$.timeframe')=?
                   AND json_extract(value,'$.result.execution_error') IS NULL
                   AND json_extract(value,'$.result.entry_filter_error') IS NULL
                   AND json_extract(value,'$.result.section_errors.buy') IS NULL
                   AND COALESCE(json_extract(value,'$.result.selection_only'),0)!=1
                   AND (json_extract(value,'$.result.sections') IS NULL
                        OR json_extract(value,'$.result.section_mode')='signals')
                   ORDER BY json_extract(summary,'$.started_at') DESC,id DESC LIMIT 1""",
                (prefix + "%", identity.strategy_id, previous, identity.asset_type, identity.timeframe),
            ).fetchone()
            archived = json.loads(row["value"]) if row else None
        current = copy.deepcopy(result)
        errors = dict(current.get("section_errors") or {})
        confirmed_zero = False
        if archived is None and is_s05:
            from app.services.daily_selections import selection_record
            from app.services.s05_selection import (
                COMMON_BRANCH_POLICY,
                NUMERIC_POLICY,
                ROUTED_POLICY,
            )

            frozen = (selection_record(identity.strategy_id, previous, load, policy=COMMON_BRANCH_POLICY)
                      or selection_record(identity.strategy_id, previous, load, policy=NUMERIC_POLICY)
                      or selection_record(identity.strategy_id, previous, load, policy=ROUTED_POLICY))
            if frozen and frozen["result"]["selection_snapshot"]["entry_branch"] is None:
                archived = frozen
        if archived is None and self.tenant:
            from app.strategy.execution_status import successful_zero_receipt

            frozen_id = replace(identity, daily_record=True).record_id(previous)
            frozen = conn.execute(
                "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                (NAMESPACE, frozen_id),
            ).fetchone()
            receipt = conn.execute(
                "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                ("critical_strategy_runs", f"{identity.strategy_id}:{previous}"),
            ).fetchone()
            confirmed_zero = successful_zero_receipt(
                receipt["value"] if receipt else {}, identity.strategy_id, previous,
                frozen["value"]["summary"].get("runtime_identity") if frozen else None,
            )
        if archived is not None and is_s05:
            from app.strategy.s02_173004552 import project_daily_buys

            source = archived["result"]
            prior = project_daily_buys(source)
            if prior is not source:
                archived = None if (prior.get("section_errors") or {}).get("buy") else {**archived, "result": prior}
        if archived is None:
            current["sections"]["sell"] = []
            if confirmed_zero:
                errors.pop("sell", None)
                current["section_statuses"] = {**(current.get("section_statuses") or {}), "sell": "success"}
            else:
                errors["sell"] = f"缺少{previous}买入信号归档。无法确认本交易日卖出关注名单"
                current["section_statuses"] = {**(current.get("section_statuses") or {}), "sell": "waiting"}
        else:
            prior = archived["result"]
            buys = (prior.get("sections") or {}).get("buy", prior.get("rows", []))
            if is_s05:
                from app.strategy.s02_173004552 import with_entry_branches

                buys = with_entry_branches(buys, prior.get("entry_signal_hits") or [])
            current["sections"]["sell"] = [
                {**item, "entry_date": previous, "entry_record_id": archived["record_id"]} for item in buys
            ]
            if (current.get("section_statuses") or {}).get("sell") != "failed":
                errors.pop("sell", None)
                if not buys:
                    current["section_statuses"] = {**(current.get("section_statuses") or {}), "sell": "success"}
        current["section_errors"] = errors
        return current

    def save(self, identity: ResultIdentity, result: dict, *, started_at: int,
             input_generation=None) -> dict:
        as_of = date.fromisoformat(str(result["as_of"])).isoformat()
        record_id = identity.record_id(as_of)
        diagnostic = (identity.strategy_id == "custom_mvxxcp88"
                      and bool((result.get("repair_provenance") or {}).get("diagnostic_only")))
        if diagnostic:
            record_id = f"{record_id[:28]}{digest([record_id, result['repair_provenance'], started_at])}"

        def comparable(value):
            # Single and batch runners omit different empty optional fields.
            defaults = {"strategy_id": identity.strategy_id, "total": 0, "rows": [],
                        "scores": {}, "warnings": [], "entry_signal_hits": [],
                        "exit_signal_hits": [], "entry_filter_error": None, "sections": None,
                        "state": None, "portfolio_scope": None, "execution_error": None,
                        "daily_stock_pool": None, "daily_stock_pool_error": None,
                        "section_mode": None, "section_errors": None}
            comparable = {**defaults, **{key: item for key, item in value.items()
                                        if key not in {"computed_at", "elapsed_ms"}}}
            if comparable.get("entry_data_quality"):
                comparable["entry_data_quality"] = copy.deepcopy(comparable["entry_data_quality"])
                late_session = False
                for quality in comparable["entry_data_quality"].values():
                    progress = quality.get("progress") or {}
                    if (progress.get("schema") != "late_session_progress_v1"
                            or progress.get("as_of") != value.get("as_of")):
                        continue
                    late_session = True
                    # Observation clocks do not change the business snapshot.
                    progress.pop("observed_at", None)
                    for step in progress.get("steps", {}).values():
                        for field in ("started_at", "finished_at", "elapsed_ms"):
                            step.pop(field, None)
                if late_session:
                    comparable.pop("last_success_at", None)
                    if comparable.get("execution_status"):
                        comparable["execution_status"] = dict(comparable["execution_status"])
                        comparable["execution_status"].pop("updated_at", None)
            return comparable

        def merged(old, conn):
            if old and result.get("selection_only"):
                from app.services.daily_selections import valid_selection
                if valid_selection(old["result"].get("selection_snapshot"), as_of,
                                   strategy_id=identity.strategy_id if identity.selection_policy else None):
                    return old, "unchanged"
            if old and old["summary"]["started_at"] > started_at:
                return old, "unchanged"
            from app.services.late_session_receipts import preserve_first_receipts
            from app.strategy.execution_status import merge_result, result_status

            prior_result = (old["result"] if old and old["summary"].get("identity") == identity.fingerprint
                            else None)

            independent_sell = ((result.get("monitor_tasks") or {}).get("sell") or {}).get("state") in (
                "success", "failed")
            from app.services.strategy_monitor_state import CONTRACT
            canonical_monitor = result.get("monitor_contract") == CONTRACT
            if old and result_status(old["result"])["state"] == "success" \
                    and result_status(result)["state"] != "success" and not independent_sell \
                    and not canonical_monitor and not result.get("selection_only"):
                shown = merge_result(old["result"], result)
                preserve_first_receipts(shown, prior_result)
                return {**copy.deepcopy(old), "result": shown}, "unchanged"
            payload = copy.deepcopy(result)
            if not canonical_monitor and independent_sell and prior_result and (prior_result.get("section_statuses") or {}).get("buy") == "success":
                payload["rows"] = copy.deepcopy(prior_result.get("rows") or [])
                payload["total"] = prior_result.get("total", 0)
                payload["sections"]["buy"] = copy.deepcopy(prior_result["sections"]["buy"])
                payload["section_statuses"]["buy"] = "success"
                payload["section_errors"].pop("buy", None)
                payload.pop("entry_filter_error", None)
                payload.pop("execution_status", None)
            if (payload.get("monitor_tasks") and prior_result
                    and (payload["monitor_tasks"].get("sell") or {}).get("state") not in ("success", "failed")):
                payload["exit_signal_hits"] = copy.deepcopy(prior_result.get("exit_signal_hits") or [])
                if ((prior_result.get("monitor_tasks") or {}).get("sell") or {}).get("state") == "success":
                    payload["section_statuses"]["sell"] = (prior_result.get("section_statuses") or {}).get("sell", "success")
            reference = payload.pop("price_suggestion_reference", None)
            if payload.get("section_mode") == "signals" and not payload.get("selection_only"):
                from app.services.daily_selections import selection_record

                def load(record_id):
                    if self.tenant:
                        row = conn.execute(
                            "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                            (NAMESPACE, record_id),
                        ).fetchone()
                        return row["value"] if row else None
                    row = conn.execute("SELECT value FROM strategy_results WHERE id=?", (record_id,)).fetchone()
                    return json.loads(row["value"]) if row else None

                frozen = selection_record(identity.strategy_id, as_of, load)
                selected = (frozen or {}).get("result", {}).get("selection_snapshot")
                if selected is not None:
                    payload["selection_snapshot"] = selected
                    payload["sections"]["select"] = selected["rows"]
            current = self._with_previous_buy(conn, identity, payload, as_of)
            if reference is not None:
                from app.strategy.signal_price_suggestions import with_price_suggestions
                current = with_price_suggestions(current, reference)
            current = json_value(current)
            preserve_first_receipts(current, prior_result)
            if old and comparable(old["result"]) == comparable(current):
                # Advance the run order even when the output is unchanged, so a
                # late older run cannot replace the latest successful result.
                summary = json_value({**old["summary"], "started_at": started_at,
                                      "input_generation": input_generation})
                if summary == old["summary"]:
                    return old, "unchanged"
                return json_value({**old, "summary": summary}), "summary"
            current["computed_at"] = int(time.time() * 1000)
            ever = {**(old["ever_rows"] if old else {}),
                    **{str(row["symbol"]): row for row in current.get("rows", [])
                       if row.get("symbol")}}
            summary = {
                **identity.snapshot(), "record_id": record_id, "as_of": as_of,
                "total": int(current.get("total") or 0), "ever_count": len(ever),
                "started_at": started_at, "computed_at": current["computed_at"],
                "input_generation": input_generation,
                "selection_only": bool(current.get("selection_only")),
                "selection_count": len((current.get("sections") or {}).get("select", [])),
            }
            if diagnostic:
                summary["diagnostic_only"] = True
            return json_value({"record_id": record_id, "summary": summary,
                               "result": current, "ever_rows": ever,
                               "_cache_token": uuid4().hex}), "result"

        if self.tenant:
            from psycopg.types.json import Jsonb
            with self.storage._transaction(write=True) as conn:
                self._check_generation(conn, identity.strategy_id, identity.storage_generation)
                if result.get("daily_stock_pool") is not None and not diagnostic:
                    from app.services.daily_stock_pools import DailyStockPools
                    DailyStockPools(self.storage).save_in_transaction(
                        conn, identity, result["daily_stock_pool"], started_at=started_at,
                        input_generation=input_generation)
                row = conn.execute(
                    "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                    (NAMESPACE, record_id),
                ).fetchone()
                value, update = merged(row["value"] if row else None, conn)
                if update == "summary":
                    conn.execute(
                        """UPDATE business.documents SET value=jsonb_set(value,'{summary}',%s),
                           revision=revision+1,updated_at=now() WHERE namespace=%s AND id=%s""",
                        (Jsonb(value["summary"]), NAMESPACE, record_id),
                    )
                elif update == "result":
                    conn.execute(
                        """INSERT INTO business.documents(tenant_id,namespace,id,value)
                           VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET
                           value=excluded.value,revision=business.documents.revision+1,updated_at=now()""",
                        (self.storage.context.tenant_id, NAMESPACE, record_id, Jsonb(value)),
                    )
                if (identity.asset_type == "stock" and identity.timeframe in ("1d", "1m")
                        and value["result"].get("section_mode") == "signals"):
                    from app.services.strategy_day_views import publish
                    from app.services.strategy_monitor_tasks import forward_buy
                    if not diagnostic:
                        forward_buy(self, conn, identity, value)
                    publish(self, conn, identity.strategy_id, as_of, identity.asset_type, identity.timeframe)
        else:
            with self._local(write=True) as conn:
                self._check_generation(conn, identity.strategy_id, identity.storage_generation)
                if result.get("daily_stock_pool") is not None and not diagnostic:
                    from app.services.daily_stock_pools import DailyStockPools
                    DailyStockPools(self.storage).save_in_transaction(
                        conn, identity, result["daily_stock_pool"], started_at=started_at,
                        input_generation=input_generation)
                row = conn.execute("SELECT value FROM strategy_results WHERE id=?", (record_id,)).fetchone()
                value, update = merged(json.loads(row["value"]) if row else None, conn)
                if update == "summary":
                    summary = _encode(value["summary"])
                    conn.execute(
                        """UPDATE strategy_results SET summary=?,value=json_set(value,'$.summary',json(?)),
                           revision=revision+1 WHERE id=?""",
                        (summary, summary, record_id),
                    )
                elif update == "result":
                    conn.execute(
                        """INSERT INTO strategy_results(id,summary,value) VALUES(?,?,?)
                           ON CONFLICT(id) DO UPDATE SET summary=excluded.summary,value=excluded.value,
                           revision=strategy_results.revision+1""",
                        (record_id, _encode(value["summary"]), _encode(value)),
                    )
                if (identity.asset_type == "stock" and identity.timeframe in ("1d", "1m")
                        and value["result"].get("section_mode") == "signals"):
                    from app.services.strategy_day_views import publish
                    from app.services.strategy_monitor_tasks import forward_buy
                    if not diagnostic:
                        forward_buy(self, conn, identity, value)
                    publish(self, conn, identity.strategy_id, as_of, identity.asset_type, identity.timeframe)
        logger.info("strategy result %s: asset=%s timeframe=%s date=%s",
                    "saved" if update == "result" else "reused",
                    identity.asset_type, identity.timeframe, as_of)
        return copy.deepcopy(value)

    def day_view(self, strategy_id, as_of, asset_type="stock", timeframe="1m"):
        from app.services.strategy_day_views import read
        return read(self, strategy_id, as_of, asset_type, timeframe)

    def read(self, identity: ResultIdentity, as_of: str, *, record_id=None) -> dict | None:
        saved = self.detail(identity.strategy_id, record_id or identity.record_id(as_of))
        from app.services.daily_selections import selection_record
        frozen = None
        from app.services.selection_clock import S03_ID, S05_ID
        if identity.timeframe in ("1d", "1m") and (
                identity.strategy_id in (S03_ID, S05_ID) or saved is None
                or not saved["result"].get("selection_snapshot")):
            frozen = selection_record(identity.strategy_id, as_of,
                                      lambda key: self.detail(identity.strategy_id, key))
        if saved is None and identity.timeframe in ("1d", "1m"):
            fallback = frozen
            if ((fallback or {}).get("result", {}).get("selection_only")
                    and fallback["summary"].get("timeframe") == identity.timeframe
                    and fallback["summary"].get("asset_type") == identity.asset_type):
                saved = fallback
        if (saved and identity.timeframe in ("1d", "1m")
                and saved["result"].get("section_mode") == "signals"
                and (not saved["result"].get("selection_snapshot") or identity.strategy_id in (S03_ID, S05_ID))
                and not saved["result"].get("selection_only")):
            selected = (frozen or {}).get("result", {}).get("selection_snapshot")
            if selected is not None:
                # A historical backfill can arrive after a missing-selection scan.
                # Merge only the read copy; keep its original buy/sell archive intact.
                saved["result"]["selection_snapshot"] = selected
                saved["result"]["sections"]["select"] = selected["rows"]
                saved["result"]["section_errors"] = {
                    key: value for key, value in (saved["result"].get("section_errors") or {}).items()
                    if key != "select"}
                if identity.strategy_id == S05_ID:
                    from app.strategy.s02_173004552 import (
                        MARKET_ROUTE,
                        project_daily_buys,
                        project_no_market_route,
                    )

                    saved["result"] = project_daily_buys(saved["result"], selected)
                    if (selected.get("entry_route") or {}).get("schema") != MARKET_ROUTE:
                        project_no_market_route(saved["result"], selected)
        if saved:
            from app.services.custom_status_refresh import result
            saved = {**saved, "result": result(self, identity.strategy_id, saved["result"])}
        return saved

    def detail(self, strategy_id: str, record_id: str) -> dict | None:
        prefix = digest(strategy_id)[:16] + ":"
        if not record_id.startswith(prefix) or len(record_id) != 92:
            return None
        if self.tenant:
            with self.storage._transaction() as conn:
                row = conn.execute(
                    """SELECT revision,value->>'_cache_token' AS cache_token
                       FROM business.documents WHERE namespace=%s AND id=%s""",
                    (NAMESPACE, record_id),
                ).fetchone()
                if not row:
                    return None
                # A deleted ID can be inserted again with revision=1 in another process.
                key = (self.scope, record_id, row["revision"], row["cache_token"])
                cached = self._hot(key) if self.cacheable else None
                if cached is not None:
                    return cached
                row = conn.execute(
                    "SELECT value,revision FROM business.documents WHERE namespace=%s AND id=%s",
                    (NAMESPACE, record_id),
                ).fetchone()
                if row is None:
                    return None
                value = row["value"]
                key = (self.scope, record_id, row["revision"], value.get("_cache_token"))
        else:
            with self._local() as conn:
                if conn is None:
                    return None
                row = conn.execute(
                    """SELECT revision,json_extract(value,'$._cache_token') AS cache_token
                       FROM strategy_results WHERE id=?""", (record_id,),
                ).fetchone()
                if not row:
                    return None
                key = (self.scope, record_id, row["revision"], row["cache_token"])
                cached = self._hot(key)
                if cached is not None:
                    return cached
                value = json.loads(conn.execute(
                    "SELECT value FROM strategy_results WHERE id=?", (record_id,),
                ).fetchone()["value"])
        if value["summary"]["strategy_id"] != strategy_id:
            return None
        if self.cacheable:
            _remember(key, value)
        return copy.deepcopy(value)

    def delete(self, strategy_id: str, record_id: str) -> bool:
        prefix = digest(strategy_id)[:16] + ":"
        if not record_id.startswith(prefix) or len(record_id) != 92:
            return False
        if self.tenant:
            with self.storage._transaction(write=True) as conn:
                deleted = conn.execute(
                    """DELETE FROM business.documents WHERE namespace=%s AND id=%s
                       AND value->'summary'->>'strategy_id'=%s""",
                    (NAMESPACE, record_id, strategy_id),
                ).rowcount > 0
                if deleted:
                    from app.services.strategy_day_views import publish
                    for timeframe in ("1d", "1m"):
                        publish(self, conn, strategy_id, record_id[17:27], timeframe=timeframe)
        else:
            if not self.path.exists():
                return False
            with self._local(write=True) as conn:
                deleted = conn.execute(
                    """DELETE FROM strategy_results WHERE id=?
                       AND json_extract(summary,'$.strategy_id')=?""",
                    (record_id, strategy_id),
                ).rowcount > 0
                if deleted:
                    from app.services.strategy_day_views import publish
                    for timeframe in ("1d", "1m"):
                        publish(self, conn, strategy_id, record_id[17:27], timeframe=timeframe)
        if deleted and self.cacheable:
            global _memory_size
            with _memory_lock:
                for key in [key for key in _memory if key[:2] == (self.scope, record_id)]:
                    _, size = _memory.pop(key)
                    _memory_size -= size
        return deleted

    @staticmethod
    def _hot(key):
        with _memory_lock:
            entry = _memory.get(key)
            if entry is not None:
                _memory.move_to_end(key)
                return json.loads(entry[0])
        return None

    def monitor_snapshot(self, *, now_ms):
        """Reject expired tenant monitoring metadata before fetching its large body."""
        if not self.tenant:
            return None
        with self.storage._transaction() as conn:
            row = conn.execute(
                """SELECT value FROM business.documents WHERE namespace='monitor_results'
                   AND id='default' AND (value->>'sequence')::numeric BETWEEN %s AND %s""",
                (now_ms - 30000, now_ms),
            ).fetchone()
        return row["value"] if row else None

    def summaries(self, identities: list[ResultIdentity], as_of: str, *,
                  signal_strategy_ids=()) -> dict:
        ids = list(dict.fromkeys(
            [identity.record_id(as_of) for identity in identities]
            + [replace(identity, daily_record=True).record_id(as_of)
               for identity in identities if identity.timeframe in ("1d", "1m")]))
        if not ids:
            return {}
        signal_ids = set(signal_strategy_ids)
        conditions, params = [], []
        placeholder = "%s" if self.tenant else "?"
        tomorrow = str(date.fromisoformat(str(as_of)) + timedelta(days=1))
        for identity in identities:
            if identity.strategy_id not in signal_ids:
                continue
            prefix = digest(identity.strategy_id)[:16] + ":"
            field = (lambda name: f"value->'summary'->>'{name}'") if self.tenant else (
                lambda name: f"json_extract(summary,'$.{name}')")
            conditions.append(
                f"(id>={placeholder} AND id<{placeholder}"
                f" AND {field('strategy_id')}={placeholder}"
                f" AND {field('asset_type')}={placeholder}"
                f" AND {field('timeframe')}={placeholder})")
            params.extend([prefix + str(as_of) + ":", prefix + tomorrow + ":",
                           identity.strategy_id, identity.asset_type, identity.timeframe])
        mode = "value->'result'->>'section_mode'" if self.tenant else (
            "json_extract(value,'$.result.section_mode')")
        historical = f" OR (({' OR '.join(conditions)}) AND {mode}='signals')" if conditions else ""
        if self.tenant:
            queries = [
                """SELECT id,value->'summary' AS summary FROM business.documents
                   WHERE namespace=%s AND id=ANY(%s)"""
            ]
            query_params = [NAMESPACE, ids]
            for index, condition in enumerate(conditions):
                queries.append(
                    f"""SELECT id,value->'summary' AS summary FROM business.documents
                        WHERE namespace=%s AND {condition} AND NOT (id=ANY(%s))
                        AND {mode}='signals'"""
                )
                query_params.extend([NAMESPACE, *params[index * 5:(index + 1) * 5], ids])
            with self.storage._transaction() as conn:
                rows = conn.execute(" UNION ALL ".join(queries), query_params).fetchall()
            values = list({row["id"]: row["summary"] for row in rows}.values())
        else:
            with self._local() as conn:
                if conn is None:
                    return {}
                rows = conn.execute(
                    f"""SELECT summary FROM strategy_results
                        WHERE (id IN ({','.join('?' for _ in ids)}){historical})""",
                    [*ids, *params],
                ).fetchall()
            values = [json.loads(row["summary"]) for row in rows]
        allowed = {(identity.strategy_id, identity.asset_type, identity.timeframe) for identity in identities}
        values = [value for value in values if (
            value.get("strategy_id"), value.get("asset_type"), value.get("timeframe")) in allowed]
        preferred = {identity.strategy_id: identity.record_id(as_of) for identity in identities}
        # Signal history belongs to the saved day, including its old configuration.
        # Eligibility-only records must not hide an actual (possibly empty) scan.
        values.sort(key=lambda value: (
            not value.get("selection_only") if value["strategy_id"] in signal_ids
            else value["record_id"] == preferred.get(value["strategy_id"]),
            value.get("started_at") or 0, value.get("computed_at") or 0, value["record_id"],
        ))
        return {value["strategy_id"]: value for value in values}

    def list_history(self, strategy_id: str, *, limit=30, cursor=None, q=None,
                     start_date=None, end_date=None, asset_type=None, timeframe=None) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("历史分页数量必须在1到100之间")
        if q and len(q) > 200:
            raise ValueError("历史搜索关键词不能超过200个字符")
        start_date = date.fromisoformat(str(start_date)).isoformat() if start_date else None
        end_date = date.fromisoformat(str(end_date)).isoformat() if end_date else None
        if start_date and end_date and start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")
        if asset_type not in (None, "", "stock"):
            raise ValueError("历史资产类型无效")
        if timeframe not in (None, "", "1d", "1m"):
            raise ValueError("历史周期无效")
        prefix = digest(strategy_id)[:16] + ":"
        if cursor and (not cursor.startswith(prefix) or len(cursor) != 92):
            raise ValueError("历史分页游标无效")
        # ASCII "~" is not an upper bound in locale-aware PostgreSQL collations.
        # The next fixed-width hex strategy prefix bounds the same PK index range.
        number = int(prefix[:16], 16)
        upper = cursor or (f"{number + 1:016x}" if number < 2**64 - 1 else "g")
        conditions, params = [], []
        summary = "value->'summary'" if self.tenant else "summary"
        placeholder = "%s" if self.tenant else "?"

        def field(name):
            return f"{summary}->>'{name}'" if self.tenant else f"json_extract(summary,'$.{name}')"

        for name, value, operator in (
            ("as_of", start_date, ">="), ("as_of", end_date, "<="),
            ("asset_type", asset_type, "="), ("timeframe", timeframe, "="),
        ):
            if value:
                conditions.append(f"{field(name)}{operator}{placeholder}")
                params.append(value)
        if q and q.strip():
            function = "strpos" if self.tenant else "instr"
            conditions.append(
                f"({function}(lower({field('strategy_name')}),{placeholder})>0 OR "
                f"{function}(lower({field('strategy_version')}),{placeholder})>0)",
            )
            params.extend([q.strip().lower()] * 2)
        where = "".join(f" AND {condition}" for condition in conditions)
        if self.tenant:
            with self.storage._transaction() as conn:
                rows = conn.execute(
                    f"""SELECT id,value->'summary' AS summary FROM business.documents
                        WHERE namespace=%s AND id>=%s AND id<%s{where} ORDER BY id DESC LIMIT %s""",
                    (NAMESPACE, prefix, upper, *params, limit + 1),
                ).fetchall()
            values = [row["summary"] for row in rows]
        else:
            with self._local() as conn:
                if conn is None:
                    return {"records": [], "next_cursor": None}
                rows = conn.execute(
                    f"""SELECT id,summary FROM strategy_results
                        WHERE id>=? AND id<?{where} ORDER BY id DESC LIMIT ?""",
                    (prefix, upper, *params, limit + 1),
                ).fetchall()
            values = [json.loads(row["summary"]) for row in rows]
        return {"records": values[:limit],
                "next_cursor": values[limit - 1]["record_id"] if len(values) > limit else None}

    def memberships(self, identities, as_of, symbols):
        ids = [identity.record_id(as_of) for identity in identities]
        if not ids or not symbols:
            return {}
        if self.tenant:
            with self.storage._transaction() as conn:
                rows = conn.execute(
                    """SELECT d.value->'summary'->>'strategy_id' AS sid,r->>'symbol' AS symbol
                       FROM business.documents d,
                       LATERAL jsonb_array_elements(d.value->'result'->'rows') r
                       WHERE d.namespace=%s AND d.id=ANY(%s) AND r->>'symbol'=ANY(%s)""",
                    (NAMESPACE, ids, sorted(symbols)),
                ).fetchall()
        else:
            with self._local() as conn:
                if conn is None:
                    return {}
                rows = conn.execute(
                    f"""SELECT json_extract(d.summary,'$.strategy_id') AS sid,
                        json_extract(r.value,'$.symbol') AS symbol
                        FROM strategy_results d,json_each(d.value,'$.result.rows') r
                        WHERE d.id IN ({','.join('?' for _ in ids)})
                        AND json_extract(r.value,'$.symbol') IN ({','.join('?' for _ in symbols)})""",
                    [*ids, *sorted(symbols)],
                ).fetchall()
        members = {symbol: set() for symbol in symbols}
        for row in rows:
            members[row["symbol"]].add(row["sid"])
        return {symbol: sorted(strategies) for symbol, strategies in members.items()}


def persist_result(storage, identity, result, *, started_at, input_generation=None):
    """Storage failures must reach callers; a successful calculation is not a successful save."""
    from app.services.s02_run_evidence import manual_review
    result = manual_review(identity.strategy_id, result, started_at)
    try:
        return StrategyResults(storage).save(identity, result, started_at=started_at,
                                             input_generation=input_generation)
    except AccessError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except Exception:
        logger.error("strategy result persistence failed")
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="策略结果保存失败, 上一份成功记录已保留, 请重试") from None
