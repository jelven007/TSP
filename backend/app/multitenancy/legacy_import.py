"""Offline full-product import, guarded incremental reconciliation and originals.

The source must be a consistent backup. Every destination query includes a
tenant predicate even when the offline administrator bypasses RLS.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from app.multitenancy.documents import json_value
from app.multitenancy.research import read_frame
from app.services.task_state import ACTIVE_TASK_STATUSES, normalize_task_status

MAX_FILE_BYTES = 64 * 1024 * 1024
PRIVATE_SKIPPED = {
    "auth.json": "identity", "preferences.json": "split_configuration",
    "secrets.json": "platform_credentials", "auth_verifications.json": "expired_verifications",
    "strategy_cache.json": "rebuildable_cache", "strategy_run_timings.json": "runtime_timings",
}
JSON_COLLECTIONS = {"custom_factors", "custom_signals", "strategy_overrides", "lots", "monitor_rules"}
JSON_SINGLE = {
    "ai_reports.json": ("ai_reports", "ai_reports.json"),
    "ai_stock_reports.json": ("ai_reports", "ai_stock_reports.json"),
    "ai_market_recaps.json": ("ai_reports", "ai_market_recaps.json"),
    "research_candidates.json": ("research_candidates", "default"),
}
KEYS = {
    "documents": ("namespace", "id"),
    "watchlist_groups": ("id",), "watchlist_items": ("symbol",),
    "watchlist_group_items": ("group_id", "symbol"), "alerts": ("id",),
    "notification_channels": ("id",),
    "monitor_rules": ("id",), "full_paper_accounts": ("id",),
    "full_paper_orders": ("account_id", "id"), "full_paper_fills": ("account_id", "id"),
    "full_paper_nav": ("account_id", "day"), "research_jobs": ("id",),
    "research_artifacts": ("job_id", "name"), "legacy_artifacts": ("path",),
}


def canonical(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(
        canonical(value), sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


@dataclass
class Snapshot:
    root: Path
    inventory: dict = field(default_factory=dict)
    managed_files: set = field(default_factory=set)

    def __post_init__(self):
        self.root = self.root.resolve(strict=True)
        self.managed_files = self.file_names()

    def file_names(self):
        files = set()
        for name in ("user_data", "paper", "strategies", "backtest_results", "research", "screener_results"):
            root = self.root / name
            if root.is_symlink():
                raise ValueError("legacy source contains an unsafe path")
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise ValueError("legacy source contains an unsafe path")
                if path.is_file():
                    files.add(path.relative_to(self.root).as_posix())
        return files

    def read(self, path: Path, category="business", *, archive=False):
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("legacy source contains an unsafe path")
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("legacy source file exceeds import limit")
        name = path.relative_to(self.root).as_posix()
        existing = self.inventory.get(name)
        sha = hashlib.sha256(data).hexdigest()
        if existing and existing["sha256"] != sha:
            raise ValueError("legacy source changed during import")
        self.inventory[name] = {"bytes": len(data), "sha256": sha,
                                "category": existing["category"] if existing else category}
        return data, name, archive

    def json(self, path, default=None):
        return json.loads(self.read(path)[0]) if path.exists() else default

    def lines(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in self.read(path)[0].splitlines() if line.strip()]

    def verify(self):
        if self.file_names() != self.managed_files:
            raise ValueError("legacy source changed during import")
        for name, entry in self.inventory.items():
            path = self.root / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("legacy source changed during import")


@dataclass
class Plan:
    snapshot: Snapshot
    rows: list = field(default_factory=list)
    paper_ids: list = field(default_factory=list)
    archived: set = field(default_factory=set)

    def add(self, table, **values):
        if table not in KEYS:
            raise ValueError("unsupported import table")
        self.rows.append((table, {key: values[key] for key in KEYS[table]}, values))

    def document(self, namespace, item_id, value):
        self.add("documents", namespace=namespace, id=item_id, value=json_value(value))

    def archive(self, path):
        data, name, _ = self.snapshot.read(path, archive=True)
        if name not in self.archived:
            self.add("legacy_artifacts", path=name, content=data, sha256=hashlib.sha256(data).hexdigest())
            self.archived.add(name)


def build_plan(root: Path, tenant_id: str, author: dict, *, executor=None, channel_map=None) -> Plan:
    import polars as pl

    from app.strategy import lots, monitor_rules, paper, paper_auto

    snapshot = Snapshot(root)
    plan = Plan(snapshot)
    channel_map = channel_map or {}
    if not isinstance(channel_map, dict) or any(
        not isinstance(name, str) or not isinstance(target, str) or not target or len(target) > 128
        for name, target in channel_map.items()
    ):
        raise ValueError("invalid legacy channel mapping")

    def mapped_channels(names):
        if not isinstance(names, list) or any(name not in channel_map for name in names):
            raise ValueError("legacy webhook defaults require explicit channel reconciliation")
        return list(dict.fromkeys(channel_map[name] for name in names))

    private = snapshot.root / "user_data"
    for path in sorted(private.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(private)
        if path.name in PRIVATE_SKIPPED and len(relative.parts) == 1:
            snapshot.read(path, PRIVATE_SKIPPED[path.name])
        elif (path.name in JSON_SINGLE or path.name in {"watchlist.parquet", "watchlist_groups.json", "alerts.jsonl"}
              or relative.parts[0] in JSON_COLLECTIONS):
            plan.archive(path)
        else:
            raise ValueError("unclassified private file; extend explicit import mapping")
    groups = snapshot.json(private / "watchlist_groups.json", [])
    if not isinstance(groups, list):
        raise ValueError("invalid legacy watchlist groups")
    for position, group in enumerate(groups):
        plan.add("watchlist_groups", id=group["id"], name=group["name"],
                 color=group.get("color", "sky"), position=position)
    watchlist = private / "watchlist.parquet"
    if watchlist.exists():
        rows = pl.read_parquet(snapshot.read(watchlist)[0]).to_dicts()
        group_ids = {group["id"] for group in groups}
        for position, row in enumerate(rows):
            values = {"symbol": row["symbol"], "note": row.get("note") or "", "position": position}
            if row.get("added_at"):
                values["added_at"] = timestamp(row["added_at"])
            plan.add("watchlist_items", **values)
            for group_id in dict.fromkeys(row.get("group_ids") or ([row["group_id"]] if row.get("group_id") else [])):
                if group_id not in group_ids:
                    raise ValueError("legacy watchlist references a missing group")
                plan.add("watchlist_group_items", group_id=group_id, symbol=row["symbol"])
    for event in snapshot.lines(private / "alerts.jsonl"):
        stamp = event.get("ts")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
            raise ValueError("invalid legacy alert")
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sha = hashlib.sha256(encoded.encode()).hexdigest()
        plan.add("alerts", id=sha[:32], dedupe_key="legacy:" + sha, event=event, created_at=timestamp(stamp))
    for name, (namespace, item_id) in JSON_SINGLE.items():
        path = private / name
        if path.exists():
            value = snapshot.json(path)
            if not isinstance(value, list):
                raise ValueError("legacy reports/candidates must be an array")
            plan.document(namespace, item_id, value)
    for namespace in sorted(JSON_COLLECTIONS):
        for path in sorted((private / namespace).glob("*.json")):
            value = snapshot.json(path)
            item_id = path.stem
            if namespace != "strategy_overrides" and value.get("id") != item_id:
                raise ValueError("legacy object/file identifier mismatch")
            if namespace == "monitor_rules":
                value = monitor_rules.normalize(value)
                monitor_rules.validate(value)
                value["webhook_channels"] = mapped_channels(value.get("webhook_channels") or [])
                plan.add("monitor_rules", id=item_id, definition={**value, "_execution": author},
                         enabled=value.get("enabled", True))
            else:
                if namespace == "lots":
                    value = lots.normalize_lot(value)
                    lots.validate(value)
                plan.document(namespace, item_id, value)
    preferences = snapshot.json(private / "preferences.json", {})
    if "review_schedule" in preferences:
        from app.multitenancy.notifications import ReviewScheduleRequest
        plan.document("review_schedule", "default", {
            **ReviewScheduleRequest(**preferences["review_schedule"]).model_dump(), **author,
        })
    if any(key.startswith("mining_") for key in preferences):
        mining = {key: preferences.get(key, default) for key, default in (
            ("mining_schedule_enabled", False), ("mining_schedule_weekday", 4),
            ("mining_budget_profile", "balanced"),
        )}
        if (not isinstance(mining["mining_schedule_enabled"], bool)
                or isinstance(mining["mining_schedule_weekday"], bool)
                or mining["mining_schedule_weekday"] not in range(5)
                or mining["mining_budget_profile"] not in {"balanced", "strict"}):
            raise ValueError("invalid legacy mining schedule")
        plan.document("preferences", "default", {
            key: value for key, value in preferences.items() if key.startswith("mining_")
        })
        plan.document("mining_schedule", "default", {**mining, **author})
    if preferences.get("webhook_default_channels") or preferences.get("review_push_mode"):
        from app.multitenancy.notifications import PreferencesRequest
        selected = mapped_channels(preferences.get("webhook_default_channels") or [])
        plan.document("notification_preferences", "default", PreferencesRequest(
            default_channels=selected, review_channels=selected,
            review_push_mode=preferences.get("review_push_mode", "manual"),
        ).model_dump())

    paper_root = snapshot.root / "paper"
    account_paths = sorted((paper_root / "accounts").glob("*/account.json"))
    if (paper_root / "account.json").exists():
        if account_paths:
            raise ValueError("mixed legacy paper layouts require reconciliation")
        account_paths = [paper_root / "account.json"]
    for account_path in account_paths:
        base = account_path.parent
        account = snapshot.json(account_path)
        account_id = account.get("id", paper.DEFAULT_ACCOUNT_ID)
        paper.validate_account_id(account_id)
        if base != paper_root and base.name != account_id:
            raise ValueError("paper account/file identifier mismatch")
        fills = snapshot.lines(base / "fills.jsonl")
        account = {**account, "id": account_id, "name": account.get("name", account_id),
                   "status": account.get("status", "active")}
        positions_value = snapshot.json(base / "positions.json")
        plan.add("full_paper_accounts", id=account_id, value=account,
                 positions=positions_value.get("positions", {}) if positions_value else None)
        plan.paper_ids.append(account_id)
        for order_path in sorted((base / "orders").glob("*.json")):
            order = snapshot.json(order_path)
            if order.get("id") != order_path.stem:
                raise ValueError("paper order/file identifier mismatch")
            plan.add("full_paper_orders", account_id=account_id, id=order["id"], value=order)
        seen_fills = set()
        for ordinal, fill in enumerate(fills, 1):
            key = (f"action:{fill['action_id']}:{fill['stage']}" if fill.get("kind") == "corporate_action"
                   else f"corp:{fill['symbol']}:{fill['date']}" if fill.get("kind") == "corp_action"
                   else f"fill:{fill['order_id']}")
            if key in seen_fills:
                raise ValueError("duplicate legacy paper fill")
            seen_fills.add(key)
            plan.add("full_paper_fills", account_id=account_id, id=key, ordinal=ordinal, value=fill)
        seen_days = set()
        for nav in snapshot.lines(base / "nav" / "daily.jsonl"):
            day = date.fromisoformat(nav["date"])
            if day in seen_days:
                raise ValueError("duplicate legacy NAV day")
            seen_days.add(day)
            plan.add("full_paper_nav", account_id=account_id, day=day, value=nav)
        for rule_path in sorted((base / "auto_rules").glob("*.json")):
            rule = paper_auto.normalize_rule(snapshot.json(rule_path))
            paper_auto.validate_rule(rule)
            plan.document(f"paper_auto:{account_id}", rule["id"], rule)
        for path in sorted(base.rglob("*")):
            if path.is_file():
                plan.archive(path)
    sources = []
    for source in ("custom", "ai", "composite"):
        for path in sorted((snapshot.root / "strategies" / source).glob("*.py")):
            if path.name == "__init__.py":
                snapshot.read(path, "package_marker")
                continue
            sources.append({"id": path.stem, "source": source, "code": snapshot.read(path)[0].decode("utf-8")})
            plan.archive(path)
    if sources and executor is None:
        raise ValueError("private source import requires the isolated compute executor")
    for value in sources:
        definition = executor.execute({
            "kind": "describe", "tenant_id": tenant_id, "sources": sources,
            "strategy_id": value["id"], "overrides": {}, "factors": [
                row[2]["value"] for row in plan.rows if row[0] == "documents" and row[2]["namespace"] == "custom_factors"
            ], "signals": [
                row[2]["value"] for row in plan.rows if row[0] == "documents" and row[2]["namespace"] == "custom_signals"
            ],
        })["definition"]
        if definition["meta"]["id"] != value["id"] or definition["source"] != value["source"]:
            raise ValueError("legacy private strategy declaration mismatch")
        plan.document("strategy_sources", value["id"], {**value, "definition": definition})
    mining_root = snapshot.root / "research" / "mining" / "runs"
    for path in sorted(mining_root.glob("*/manifest.json")):
        manifest = snapshot.json(path)
        run_id = manifest["run_id"]
        if run_id != path.parent.name:
            raise ValueError("legacy mining run/file identifier mismatch")
        original_state = normalize_task_status(manifest["status"])
        state = "interrupted" if original_state in ACTIVE_TASK_STATUSES else original_state
        summary = snapshot.json(path.parent / "summary.json", {})
        events = snapshot.lines(path.parent / "events.jsonl")
        artifact_names = []
        artifact_rows = []
        for name, relative in manifest.get("artifacts", {}).items():
            if name not in {"factors", "correlation", "candidates", "folds"}:
                raise ValueError("unknown legacy mining artifact")
            artifact_path = path.parent / relative
            data = snapshot.read(artifact_path)[0]
            read_frame(data)
            artifact_names.append(name)
            artifact_rows.append((name, data))
        plan.add("research_jobs", id=run_id, user_id=author["user_id"], kind="mining",
                 input={"tenant_id": tenant_id, "kind": "mining", "config": {
                     "run_id": run_id, "request": manifest["request"],
                     "data_fingerprint": manifest.get("data_fingerprint"), "source": "legacy",
                 }}, state=state, result={**summary, "artifacts": artifact_names},
                 signature=manifest["run_signature"], auth_version=author["auth_version"],
                 metadata={**author, "source": "legacy", "original_status": original_state,
                           "started_at": manifest.get("started_at"), "finished_at": manifest.get("finished_at")},
                 events=events[-256:], event_seq=max((event["id"] for event in events), default=0),
                 error=manifest.get("error"), created_at=timestamp(manifest["created_at"]),
                 updated_at=timestamp(manifest.get("updated_at") or manifest["created_at"]))
        for name, content in artifact_rows:
            plan.add("research_artifacts", job_id=run_id, name=name, content=content)
    for name in ("backtest_results", "research", "screener_results"):
        for path in sorted((snapshot.root / name).rglob("*")):
            if path.is_file():
                plan.archive(path)
    if snapshot.managed_files != set(snapshot.inventory):
        raise ValueError("unclassified legacy file; extend explicit import mapping")
    return plan


def _where(tenant_id, key):
    values = {"tenant_id": tenant_id, **key}
    return (sql.SQL(" AND ").join(sql.SQL("{}=%s").format(sql.Identifier(name)) for name in values),
            list(values.values()))


def _existing(conn, tenant_id, table, key, columns):
    if table not in KEYS:
        raise ValueError("unsupported import table")
    predicate, args = _where(tenant_id, key)
    return conn.execute(sql.SQL("SELECT {} FROM business.{} WHERE {} FOR UPDATE").format(
        sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table), predicate,
    ), args).fetchone()


def _adapt(value):
    return Jsonb(value) if isinstance(value, (dict, list)) else value


def apply_plan(conn, tenant_id, plan):
    """Apply only identical/adoptable records or our unchanged prior imports."""
    previous = conn.execute(
        "SELECT * FROM migration.full_import_records WHERE tenant_id=%s FOR UPDATE", (tenant_id,),
    ).fetchall()
    owned = {(row["table_name"], row["key_hash"]): row for row in previous}
    desired = {(table, digest(key)) for table, key, _ in plan.rows}
    if len(desired) != len(plan.rows):
        raise ValueError("duplicate destination key in legacy input")
    # Every previously imported value must still equal our recorded commit.
    # This also catches preview edits and prevents deleting tenant-owned work.
    for row in previous:
        current = _existing(conn, tenant_id, row["table_name"], row["key"], row["columns"])
        if current is None or digest(current) != row["target_hash"]:
            raise ValueError("imported destination changed; reconcile before retry")
    order = {name: position for position, name in enumerate(KEYS)}
    for row in sorted(previous, key=lambda item: order[item["table_name"]], reverse=True):
        if (row["table_name"], row["key_hash"]) not in desired:
            if row["table_name"] == "full_paper_fills":
                raise ValueError("source removed an immutable ledger entry")
            predicate, args = _where(tenant_id, row["key"])
            conn.execute(sql.SQL("DELETE FROM business.{} WHERE {}").format(
                sql.Identifier(row["table_name"]), predicate,
            ), args)
            conn.execute(
                "DELETE FROM migration.full_import_records WHERE tenant_id=%s AND table_name=%s AND key_hash=%s",
                (tenant_id, row["table_name"], row["key_hash"]),
            )
    counts = {}
    for table, key, values in plan.rows:
        marker = (table, digest(key))
        columns = list(values)
        current = _existing(conn, tenant_id, table, key, columns)
        if current and marker not in owned and digest(current) != digest(values):
            # V4 watchlist import preserved the original facts but had no
            # full-product ordering column. Adopt only those identical facts.
            ordering_only = (table == "watchlist_items" and current.get("position") == 0
                             and {k: v for k, v in current.items() if k != "position"}
                             == {k: v for k, v in values.items() if k != "position"})
            channel_author_only = (table == "notification_channels"
                                   and all(current[k] == values[k] for k in (
                                       "id", "name", "kind", "destination", "enabled", "user_id", "token_id",
                                   )))
            if not (ordering_only or channel_author_only):
                raise ValueError("legacy/destination content conflict")
        if table == "full_paper_fills" and current and digest(current) != digest(values):
            raise ValueError("source modified an immutable ledger entry")
        if not current:
            insert = {"tenant_id": tenant_id, **values}
            conn.execute(sql.SQL("INSERT INTO business.{} ({}) VALUES ({})").format(
                sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, insert)),
                sql.SQL(",").join(sql.Placeholder() for _ in insert),
            ), [_adapt(value) for value in insert.values()])
        elif digest(current) != digest(values):
            predicate, args = _where(tenant_id, key)
            conn.execute(sql.SQL("UPDATE business.{} SET {} WHERE {}").format(
                sql.Identifier(table),
                sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in values), predicate,
            ), [*[_adapt(value) for value in values.values()], *args])
        committed = _existing(conn, tenant_id, table, key, columns)
        if digest(committed) != digest(values):
            raise ValueError("legacy destination reconciliation failed")
        conn.execute(
            """INSERT INTO migration.full_import_records
               (tenant_id,table_name,key_hash,key,columns,target_hash) VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(tenant_id,table_name,key_hash) DO UPDATE SET
               columns=excluded.columns,target_hash=excluded.target_hash""",
            (tenant_id, table, marker[1], Jsonb(canonical(key)), columns, digest(committed)),
        )
        counts[table] = counts.get(table, 0) + 1
    return counts


def import_full_product(control, data, root, tenant_id, owner_id, *, executor=None, channel_map=None):
    from app.cell.admin import import_full_accounts
    from app.multitenancy.documents import TenantDocuments
    from app.multitenancy.paper_storage import PaperSession
    from app.strategy import paper

    with control.transaction():
        tenant = control.execute(
            "SELECT * FROM control.tenants WHERE id=%s AND status='suspended' FOR UPDATE", (tenant_id,),
        ).fetchone()
        owner = control.execute(
            "SELECT role FROM control.memberships WHERE tenant_id=%s AND user_id=%s", (tenant_id, owner_id),
        ).fetchone()
        if not tenant or not owner or owner["role"] != "owner":
            raise ValueError("full import requires an explicitly suspended tenant and owner")
        initial = Snapshot(Path(root))
        for name in ("auth.json", "preferences.json"):
            path = initial.root / "user_data" / name
            if path.exists():
                initial.read(path)
        identity_result = import_full_accounts(control, Path(root) / "user_data", tenant_id, owner_id)
        version = control.execute("SELECT auth_version FROM control.users WHERE id=%s", (owner_id,)).fetchone()
        author = {"user_id": owner_id, "auth_version": version["auth_version"],
                  "policy_version": tenant["policy_version"] + 1, "routing_epoch": tenant["routing_epoch"]}
        plan = build_plan(Path(root), tenant_id, author, executor=executor, channel_map=channel_map)
        if initial.managed_files != plan.snapshot.managed_files:
            raise ValueError("legacy source changed during import")
        for name, entry in initial.inventory.items():
            if plan.snapshot.inventory[name]["sha256"] != entry["sha256"]:
                raise ValueError("legacy source changed during import")
        with data.transaction():
            data.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            identity = data.execute("SELECT cell_id FROM business.cell_identity").fetchone()
            binding = data.execute(
                "SELECT routing_epoch FROM business.tenant_bindings WHERE tenant_id=%s FOR UPDATE", (tenant_id,),
            ).fetchone()
            if (not identity or identity["cell_id"] != tenant["cell_id"]
                    or not binding or binding["routing_epoch"] != tenant["routing_epoch"]):
                raise ValueError("legacy import has a missing/stale Cell binding")
            # The retired Cell model has different order/automation contracts.
            # Never silently accept a snapshot containing unconverted activity.
            for table in ("strategies", "strategy_jobs", "paper_accounts", "paper_orders", "paper_fills",
                          "trade_automations", "trade_intents"):
                if data.execute(sql.SQL("SELECT 1 FROM business.{} WHERE tenant_id=%s LIMIT 1").format(
                    sql.Identifier(table),
                ), (tenant_id,)).fetchone():
                    raise ValueError("Cell-only private activity requires explicit domain conversion")
            for channel in data.execute(
                "SELECT * FROM business.notification_channels WHERE tenant_id=%s", (tenant_id,),
            ).fetchall():
                user = control.execute(
                    "SELECT email,auth_version FROM control.users WHERE id=%s", (channel["user_id"],),
                ).fetchone()
                if (not user or (channel["kind"] == "email" and channel["destination"] != user["email"])
                        or channel["token_id"]):
                    raise ValueError("legacy notification channel author/destination requires reconciliation")
                plan.add("notification_channels", **{k: channel[k] for k in (
                    "id", "name", "kind", "destination", "enabled", "user_id", "token_id",
                )}, auth_version=user["auth_version"], policy_version=author["policy_version"],
                         routing_epoch=author["routing_epoch"])
            available = {key["id"]: value for table, key, value in plan.rows if table == "notification_channels"}
            for name, target in (channel_map or {}).items():
                channel = available.get(target)
                if not channel or channel["kind"] != ("email" if name == "email" else "webhook"):
                    raise ValueError("legacy channel mapping has a missing or mismatched tenant destination")
            counts = apply_plan(data, tenant_id, plan)
            for account_id in plan.paper_ids:
                # The existing pure replay routine is the ledger authority.
                # This object is used only for its already locked SQL reads.
                documents = TenantDocuments(None, type("Scope", (), {"tenant_id": tenant_id})())
                session = PaperSession(documents, data, False)
                positions, cash = paper.replay_positions(session, account_id=account_id)
                account = session.account(account_id)
                if abs(float(account["cash"]) - cash) > .02:
                    raise ValueError("legacy paper cash disagrees with ledger replay")
                stored = session.positions(account_id)
                if stored is not None:
                    for symbol in set(stored) | set(positions):
                        for column in ("qty", "avg_cost"):
                            if abs(float(stored.get(symbol, {}).get(column, 0))
                                   - float(positions.get(symbol, {}).get(column, 0))) > .00001:
                                raise ValueError("legacy paper positions disagree with ledger replay")
            plan.snapshot.verify()
            data.execute(
                """INSERT INTO migration.full_import_batches(tenant_id,source_hash,inventory) VALUES(%s,%s,%s)
                   ON CONFLICT(tenant_id) DO UPDATE SET source_hash=excluded.source_hash,
                   inventory=excluded.inventory,imported_at=now()""",
                (tenant_id, digest(plan.snapshot.inventory), Jsonb(plan.snapshot.inventory)),
            )
    return {**identity_result, "rows": counts, "files": len(plan.snapshot.inventory),
            "source_hash": digest(plan.snapshot.inventory), "channel_map": channel_map or {}, "reconciled": True}


def verify_import(data, root, tenant_id):
    """Read-only content reconciliation for a committed source/destination."""
    batch = data.execute(
        "SELECT * FROM migration.full_import_batches WHERE tenant_id=%s", (tenant_id,),
    ).fetchone()
    if not batch:
        raise ValueError("full import has not committed")
    snapshot = Snapshot(Path(root), batch["inventory"])
    if snapshot.managed_files != set(snapshot.inventory):
        raise ValueError("legacy source file inventory differs")
    snapshot.verify()
    counts = {}
    with data.transaction():
        for row in data.execute(
            "SELECT * FROM migration.full_import_records WHERE tenant_id=%s", (tenant_id,),
        ).fetchall():
            current = _existing(data, tenant_id, row["table_name"], row["key"], row["columns"])
            if current is None or digest(current) != row["target_hash"]:
                raise ValueError("imported destination content differs")
            counts[row["table_name"]] = counts.get(row["table_name"], 0) + 1
    return {"reconciled": True, "rows": counts, "files": len(snapshot.inventory),
            "source_hash": batch["source_hash"]}
