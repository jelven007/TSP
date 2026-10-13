from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextlib import nullcontext
from datetime import date

from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from app.cell.context import AccessError, TenantContext
from app.cell.database import Databases
from app.cell.watchlist import Watchlist


def input_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


class Strategies:
    def __init__(self, db: Databases):
        self.db = db

    @staticmethod
    def catalog(conn) -> list[dict]:
        return conn.execute("SELECT id, definition, code_hash FROM control.strategy_catalog ORDER BY id").fetchall()

    @staticmethod
    def validate_overrides(definition: dict, overrides: dict) -> None:
        allowed = {"params", "basic_filter", "scoring", "display_limit"}
        if set(overrides) - allowed:
            raise AccessError(422, "unsupported_strategy_override")
        if any(not isinstance(overrides.get(key, {}), dict) for key in ("params", "basic_filter", "scoring")):
            raise AccessError(422, "invalid_strategy_config")
        if len(json.dumps(overrides, allow_nan=False)) > 20000:
            raise AccessError(422, "strategy_config_too_large")
        params = {p["id"]: p for p in definition.get("params", [])}
        for key, value in overrides.get("params", {}).items():
            param = params.get(key)
            if not param:
                raise AccessError(422, "unknown_strategy_parameter")
            kind = param["type"]
            if kind == "bool":
                valid = isinstance(value, bool)
            elif kind in {"float", "int"}:
                valid = not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value)
                if kind == "int":
                    valid = valid and isinstance(value, int)
                valid = valid and value >= param.get("min", -math.inf) and value <= param.get("max", math.inf)
            else:
                valid = isinstance(value, str) and (kind != "select" or value in param.get("options", []))
            if not valid:
                raise AccessError(422, "invalid_strategy_parameter")
        basic = overrides.get("basic_filter", {})
        if set(basic) - set(definition.get("basic_filter", {})) - {"enabled"}:
            raise AccessError(422, "unsupported_basic_filter")
        for key, value in basic.items():
            if key in {"enabled", "exclude_st"}:
                valid = isinstance(value, bool)
            elif key == "boards":
                valid = isinstance(value, list) and set(value) <= {"沪主板", "深主板", "创业板", "科创板"}
            else:
                valid = value is None or (not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value) and value >= 0)
            if not valid:
                raise AccessError(422, "invalid_basic_filter")
        scoring = overrides.get("scoring", {})
        if set(scoring) - set(definition.get("scoring", {})):
            raise AccessError(422, "unsupported_scoring_field")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in scoring.values()):
            raise AccessError(422, "invalid_scoring_weight")
        limit = overrides.get("display_limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise AccessError(422, "invalid_display_limit")

    def list(self, ctx: TenantContext) -> list[dict]:
        with self.db.business_transaction(ctx, permission="strategies:read") as conn:
            return conn.execute("SELECT * FROM business.strategies ORDER BY updated_at DESC, id").fetchall()

    def save(self, ctx: TenantContext, catalog: list[dict], template_id: str, name: str, overrides: dict, strategy_id: str | None = None, version: int | None = None) -> dict:
        template = next((row for row in catalog if row["id"] == template_id), None)
        if not template:
            raise AccessError(404, "template_not_found")
        self.validate_overrides(template["definition"], overrides)
        with self.db.business_transaction(ctx, write=True, permission="strategies:write") as conn:
            if strategy_id:
                current = conn.execute("SELECT * FROM business.strategies WHERE id = %s FOR UPDATE", (strategy_id,)).fetchone()
                if not current:
                    raise AccessError(404, "strategy_not_found")
                if current["version"] != version:
                    raise AccessError(409, "strategy_version_stale")
                saved = conn.execute(
                    """UPDATE business.strategies SET name=%s, template_id=%s, template_hash=%s,
                       overrides=%s, version=version+1, updated_at=now() WHERE id=%s RETURNING *""",
                    (name, template_id, template["code_hash"], Jsonb(overrides), strategy_id),
                ).fetchone()
            else:
                if conn.execute("SELECT count(*) AS n FROM business.strategies").fetchone()["n"] >= 100:
                    raise AccessError(429, "strategy_limit")
                strategy_id = uuid.uuid4().hex
                saved = conn.execute(
                    """INSERT INTO business.strategies(tenant_id,id,template_id,template_hash,name,overrides)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (ctx.tenant_id, strategy_id, template_id, template["code_hash"], name, Jsonb(overrides)),
                ).fetchone()
            Watchlist._audit(conn, ctx, "strategy.save", strategy_id)
            return saved

    def delete(self, ctx: TenantContext, strategy_id: str) -> None:
        try:
            with self.db.business_transaction(ctx, write=True, permission="strategies:write") as conn:
                if not conn.execute("DELETE FROM business.strategies WHERE id=%s", (strategy_id,)).rowcount:
                    raise AccessError(404, "strategy_not_found")
                Watchlist._audit(conn, ctx, "strategy.delete", strategy_id)
        except ForeignKeyViolation as exc:
            raise AccessError(409, "strategy_has_jobs") from exc

    def enqueue(self, ctx: TenantContext, strategy_id: str, as_of: date, key: str, control, *, connection=None, automation=None) -> dict:
        request_hash = input_hash({"strategy_id": strategy_id, "as_of": str(as_of)})
        with (nullcontext(connection) if connection else self.db.business_transaction(ctx, write=True, permission="strategies:write")) as conn:
            existing = conn.execute("SELECT * FROM business.strategy_jobs WHERE idempotency_key=%s", (key,)).fetchone()
            if existing:
                if existing["input_hash"] != request_hash:
                    raise AccessError(409, "idempotency_conflict")
                return existing
            strategy = conn.execute("SELECT * FROM business.strategies WHERE id=%s", (strategy_id,)).fetchone()
            if not strategy:
                raise AccessError(404, "strategy_not_found")
            if conn.execute("SELECT 1 FROM business.strategy_jobs WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone():
                raise AccessError(429, "tenant_job_limit")
            symbols = [row["symbol"] for row in conn.execute("SELECT symbol FROM business.watchlist_items").fetchall()]
            if not symbols or len(symbols) > 1000:
                raise AccessError(422, "strategy_requires_watchlist")
            # Catalog is platform-controlled; freeze its required history along with the config.
            template = control.execute(
                "SELECT definition FROM control.strategy_catalog WHERE id=%s", (strategy["template_id"],)
            ).fetchone()
            if not template:
                raise AccessError(409, "template_not_found")
            payload = {"strategy": strategy, "as_of": str(as_of), "symbols": sorted(symbols),
                       "submitter_token_id": ctx.token_id,
                       "history_bars": template["definition"].get("history_bars", 120)}
            job = conn.execute(
                """INSERT INTO business.strategy_jobs
                   (tenant_id,id,strategy_id,user_id,routing_epoch,policy_version,strategy_version,
                    idempotency_key,input_hash,input,automation_id,automation_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (ctx.tenant_id, uuid.uuid4().hex, strategy_id, ctx.user_id, ctx.routing_epoch,
                 ctx.policy_version, strategy["version"], key, request_hash,
                 Jsonb(json.loads(json.dumps(payload, default=str))),
                 automation["id"] if automation else None, automation["version"] if automation else None),
            ).fetchone()
            Watchlist._audit(conn, ctx, "strategy.enqueue", job["id"])
            return job

    def jobs(self, ctx: TenantContext) -> list[dict]:
        with self.db.business_transaction(ctx, permission="strategies:read") as conn:
            return conn.execute(
                """SELECT id,strategy_id,status,strategy_version,created_at,started_at,finished_at,
                          error,result FROM business.strategy_jobs ORDER BY created_at DESC LIMIT 50"""
            ).fetchall()

    def cancel(self, ctx: TenantContext, job_id: str) -> None:
        with self.db.business_transaction(ctx, write=True, permission="strategies:write") as conn:
            job = conn.execute("SELECT status FROM business.strategy_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if not job:
                raise AccessError(404, "job_not_found")
            if job["status"] in {"queued", "running"}:
                conn.execute(
                    "UPDATE business.strategy_jobs SET status='cancelling' WHERE id=%s", (job_id,)
                )
