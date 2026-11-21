"""Controlled administrator business defaults, validated before atomic seeding."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

NAMESPACES = {
    "strategy_sources", "strategy_overrides", "custom_factors", "custom_signals",
    "strategy_monitoring", "strategy_pool_preferences",
}
MARKER_NAMESPACE = "bootstrap_business_defaults"


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode()


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    value: dict


class MonitorRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    enabled: bool = Field(strict=True)
    definition: dict


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_username: str = Field(min_length=1, max_length=128)
    source: dict
    documents: list[Document] = Field(max_length=1000)
    monitor_rules: list[MonitorRule] = Field(max_length=200)
    factor_catalog: list[dict] = Field(max_length=2000)
    code_hashes: dict[str, str]
    backtest: dict


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1]
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload: Payload


def load_snapshot(path: Path) -> Snapshot:
    raw = path.read_bytes()
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("business defaults too large")
    snapshot = Snapshot.model_validate_json(raw)
    if hashlib.sha256(canonical(snapshot.payload.model_dump())).hexdigest() != snapshot.snapshot_id:
        raise ValueError("business defaults digest mismatch")
    identities = [(row.namespace, row.id) for row in snapshot.payload.documents]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate business default document")
    if any(namespace not in NAMESPACES for namespace, _ in identities):
        raise ValueError("unsupported business defaults namespace")
    ids = [rule.id for rule in snapshot.payload.monitor_rules]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate business default monitor rule")
    for name, digest in snapshot.payload.code_hashes.items():
        if (not re.fullmatch(r"[a-f0-9]{64}", digest) or Path(name).is_absolute()
                or ".." in Path(name).parts):
            raise ValueError("invalid business defaults code identity")
    return snapshot


def validate_snapshot(snapshot: Snapshot, executor, tenant_id: str) -> None:
    """Reuse private registries and the existing credential-free Compute path."""
    from app.factors.registry import all_factors, private_factor_registry
    from app.strategy import custom_signals, targets
    from app.strategy.config import validate_basic_filter

    documents = snapshot.payload.documents
    values = {(row.namespace, row.id): row.value for row in documents}
    sources = [row.value for row in documents if row.namespace == "strategy_sources"]
    source_ids = {row.id for row in documents if row.namespace == "strategy_sources"}
    factors = [row.value for row in documents if row.namespace == "custom_factors"]
    signals = [row.value for row in documents if row.namespace == "custom_signals"]
    signal_ids = {row["id"] for row in signals}
    for row in documents:
        if row.namespace in {"strategy_sources", "custom_factors", "custom_signals"} and row.value.get("id") != row.id:
            raise ValueError("business default document identity mismatch")
        if row.namespace in {"strategy_overrides", "strategy_monitoring"} and row.id not in source_ids:
            raise ValueError("business default strategy dependency missing")
    with private_factor_registry(factors, strict=True), custom_signals.private_signal_registry(signals):
        catalog = json.loads(json.dumps([asdict(s) for s in all_factors()],
                                       default=lambda v: sorted(v) if isinstance(v, frozenset) else v))
        if canonical(sorted(catalog, key=lambda r: r["id"])) != canonical(
            sorted(snapshot.payload.factor_catalog, key=lambda r: r["id"]),
        ):
            raise ValueError("business default factor catalog mismatch")
        factor_ids = {row["id"] for row in catalog}
        for signal in signals:
            custom_signals.validate(signal)
        for source in sources:
            if source.get("source") not in {"custom", "ai", "composite"} or not isinstance(source.get("code"), str):
                raise ValueError("invalid business default private source")
            result = executor.execute({
                "kind": "describe", "tenant_id": tenant_id, "sources": sources,
                "strategy_id": source["id"], "overrides": {}, "factors": factors, "signals": signals,
            })["definition"]
            if (result["meta"]["id"] != source["id"] or result["source"] != source["source"]
                    or canonical(result) != canonical(source["definition"])):
                raise ValueError("business default source declaration mismatch")
            override = values.get(("strategy_overrides", source["id"]), {})
            effective = {**source["definition"], **override,
                         "basic_filter": {**(source["definition"].get("basic_filter") or {}),
                                          **(override.get("basic_filter") or {})}}
            targets.validate(effective, set())
            validate_basic_filter(effective["basic_filter"])
            required = set(source["definition"].get("required_features") or [])
            required.update(effective.get("entry_signals") or [])
            required.update(effective.get("exit_signals") or [])
            if any(name.startswith(prefix) and name[len(prefix):] not in signal_ids
                   for name in required for prefix in (custom_signals.PREFIX, custom_signals.INTRADAY_PREFIX)):
                raise ValueError("business default custom signal missing")
            scoring = override.get("scoring") or source["definition"].get("meta", {}).get("scoring") or {}
            if not set(scoring) <= factor_ids:
                raise ValueError("business default scoring factor missing")
    rules = {row.id: row for row in snapshot.payload.monitor_rules}
    for rule in rules.values():
        definition = rule.definition
        if (definition.get("id") != rule.id or definition.get("type") != "strategy"
                or definition.get("strategy_id") not in source_ids
                or definition.get("enabled") != rule.enabled):
            raise ValueError("business default monitor identity mismatch")
        if definition.get("webhook_channels") or definition.get("webhook_url"):
            raise ValueError("business defaults contain unsupported notification dependency")
        if definition.get("scope") == "watchlist_group":
            raise ValueError("business default monitor group missing")
    for row in documents:
        if row.namespace == "strategy_monitoring":
            rule = rules.get(row.value.get("rule_id"))
            if (not rule or rule.definition["strategy_id"] != row.id
                    or row.value.get("enabled") != rule.enabled):
                raise ValueError("business default strategy monitor binding mismatch")
        if row.namespace == "strategy_pool_preferences" and (
            row.id != "default" or not set(row.value.get("ids") or []) <= source_ids
        ):
            raise ValueError("business default selected strategy missing")


def seed_business(conn, job: dict, snapshot: Snapshot) -> None:
    """Tenant binding is locked by initialize_tenant; marker and data commit together."""
    existing = conn.execute(
        "SELECT value FROM business.documents WHERE namespace=%s AND id='default'", (MARKER_NAMESPACE,),
    ).fetchone()
    if existing:
        return
    conn.execute("SELECT set_config('app.user_id',%s,true)", (job["user_id"],))
    for row in snapshot.payload.documents:
        value = copy.deepcopy(row.value)
        document_id = job["user_id"] if row.namespace == "strategy_pool_preferences" else row.id
        if row.namespace == "strategy_pool_preferences":
            value = {"ids": value["ids"], "revision": 1, "legacy_import": False}
        if row.namespace == "strategy_monitoring":
            value = {key: value[key] for key in ("strategy_id", "enabled", "rule_id", "contract") if key in value}
            value["generation"] = 1
        conn.execute(
            "INSERT INTO business.documents(tenant_id,namespace,id,value) VALUES(%s,%s,%s,%s)",
            (job["tenant_id"], row.namespace, document_id, Jsonb(value)),
        )
    for rule in snapshot.payload.monitor_rules:
        definition = {key: value for key, value in rule.definition.items()
                      if key not in {"_execution", "_system_task"}}
        definition["_execution"] = {
            "user_id": job["user_id"], "auth_version": job["auth_version"],
            "policy_version": job["policy_version"], "routing_epoch": job["routing_epoch"],
        }
        conn.execute(
            "INSERT INTO business.monitor_rules(tenant_id,id,definition,enabled) VALUES(%s,%s,%s,%s)",
            (job["tenant_id"], rule.id, Jsonb(definition), rule.enabled),
        )
    receipt = {"snapshot_id": snapshot.snapshot_id, "source": snapshot.payload.source,
               "backtest": snapshot.payload.backtest, "documents": len(snapshot.payload.documents),
               "monitor_rules": len(snapshot.payload.monitor_rules)}
    conn.execute(
        "INSERT INTO business.documents(tenant_id,namespace,id,value) VALUES(%s,%s,'default',%s)",
        (job["tenant_id"], MARKER_NAMESPACE, Jsonb(receipt)),
    )


def initialization_defaults(identity, account: dict, documents) -> dict:
    """Only the designated administrator's own space can read installed defaults."""
    empty = {"snapshot_id": None, "backtest": None}
    if account["role"] != "admin" or documents.context.tenant_id != account["default_tenant_id"]:
        return empty
    saved = documents.get(MARKER_NAMESPACE, "default")
    if saved:
        return {key: saved[key] for key in ("snapshot_id", "backtest")}
    path = identity.db.settings.bootstrap_business_defaults_path
    if path is None:
        return empty
    snapshot = load_snapshot(path)
    if account["username"] != snapshot.payload.target_username:
        return empty
    return {"snapshot_id": snapshot.snapshot_id, "backtest": snapshot.payload.backtest}
