import operator
import uuid
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from app.cell.context import AccessError, TenantContext
from app.cell.database import Databases
from app.cell.market import MarketQuote
from app.cell.watchlist import Watchlist

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def append_alert(conn, tenant_id: str, key: str, event: dict) -> None:
    created = conn.execute(
        """INSERT INTO business.alerts(tenant_id,id,dedupe_key,event) VALUES (%s,%s,%s,%s)
           ON CONFLICT (tenant_id,dedupe_key) DO NOTHING RETURNING id""",
        (tenant_id, uuid.uuid4().hex, key, Jsonb(event)),
    ).fetchone()
    if created:
        conn.execute(
            """INSERT INTO business.notification_outbox(tenant_id,id,channel_id,channel_version,alert_id)
               SELECT tenant_id,%s || ':' || id,id,version,%s FROM business.notification_channels WHERE enabled""",
            (created["id"], created["id"]),
        )


class Alerts:
    def __init__(self, db: Databases):
        self.db = db

    def rules(self, ctx: TenantContext) -> list[dict]:
        with self.db.business_transaction(ctx, permission="alerts:read") as conn:
            return conn.execute("SELECT id,definition,enabled,version FROM business.monitor_rules ORDER BY id").fetchall()

    def save_rule(self, ctx: TenantContext, rule: dict) -> dict:
        with self.db.business_transaction(ctx, write=True, permission="alerts:write") as conn:
            rule_id = rule.get("id") or uuid.uuid4().hex
            if rule.get("id") and not conn.execute("SELECT 1 FROM business.monitor_rules WHERE id=%s", (rule_id,)).fetchone():
                raise AccessError(404, "monitor_rule_not_found")
            if not rule.get("id") and conn.execute("SELECT count(*) AS n FROM business.monitor_rules").fetchone()["n"] >= 100:
                raise AccessError(429, "monitor_rule_limit")
            result = conn.execute(
                """INSERT INTO business.monitor_rules(tenant_id,id,definition,enabled)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (tenant_id,id) DO UPDATE
                   SET definition=excluded.definition,enabled=excluded.enabled,
                       version=business.monitor_rules.version+1 RETURNING *""",
                (ctx.tenant_id, rule_id, Jsonb(rule), rule["enabled"]),
            ).fetchone()
            Watchlist._audit(conn, ctx, "alert.rule.save", rule_id)
            return result

    def delete_rule(self, ctx: TenantContext, rule_id: str) -> None:
        with self.db.business_transaction(ctx, write=True, permission="alerts:write") as conn:
            conn.execute("DELETE FROM business.monitor_rules WHERE id=%s", (rule_id,))
            Watchlist._audit(conn, ctx, "alert.rule.delete", rule_id)

    def inbox(self, ctx: TenantContext, limit: int) -> list[dict]:
        with self.db.business_transaction(ctx, permission="alerts:read") as conn:
            return conn.execute(
                "SELECT id,event,created_at,read_at FROM business.alerts ORDER BY created_at DESC LIMIT %s",
                (limit,),
            ).fetchall()

    def mark_read(self, ctx: TenantContext, alert_id: str) -> None:
        with self.db.business_transaction(ctx, write=True, permission="alerts:write") as conn:
            conn.execute("UPDATE business.alerts SET read_at=now() WHERE id=%s", (alert_id,))

    @staticmethod
    def evaluate(conn, ctx: TenantContext, quotes: dict[str, MarketQuote]) -> int:
        triggered = 0
        rules = conn.execute("SELECT * FROM business.monitor_rules WHERE enabled FOR UPDATE").fetchall()
        states = {(row["rule_id"], row["symbol"]): row for row in conn.execute(
            """SELECT s.* FROM business.monitor_state s JOIN business.monitor_rules r
               ON r.tenant_id=s.tenant_id AND r.id=s.rule_id
               WHERE r.enabled AND (r.definition->'symbols') ? s.symbol FOR UPDATE OF s"""
        ).fetchall()} if rules else {}
        updates = []
        now = datetime.now(UTC)
        for row in rules:
            rule = row["definition"]
            for symbol in dict.fromkeys(rule["symbols"]):
                quote = quotes.get(symbol)
                if not quote or not quote.fresh():
                    continue
                state = states.get((row["id"], symbol), {"rule_version": row["version"], "last_sequence": -1,
                                                        "last_triggered_at": None})
                if state["rule_version"] == row["version"] and state["last_sequence"] >= quote.sequence:
                    continue
                last = state["last_triggered_at"] if state["rule_version"] == row["version"] else None
                hit = OPS[rule["op"]](getattr(quote, rule["field"]), rule["value"])
                cooled = last is None or now - last >= timedelta(seconds=rule["cooldown_seconds"])
                if hit and cooled:
                    append_alert(conn, ctx.tenant_id, f"rule:{row['id']}:{row['version']}:{symbol}:{quote.sequence}", {
                        "source": "rule", "rule_id": row["id"], "symbol": symbol,
                        "message": f"{rule['name']}: {quote.name or symbol}",
                        "price": quote.price, "change_pct": quote.change_pct, "severity": rule["severity"],
                    })
                    last = now
                    triggered += 1
                updates.append((ctx.tenant_id, row["id"], symbol, row["version"], quote.sequence, last))
        if updates:
            # Psycopg pipelines executemany: one bounded write pass per tenant, no
            # insert/select/update round trip per rule-symbol pair.
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO business.monitor_state(tenant_id,rule_id,symbol,rule_version,last_sequence,last_triggered_at)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,rule_id,symbol) DO UPDATE SET
                       rule_version=excluded.rule_version,last_sequence=excluded.last_sequence,
                       last_triggered_at=excluded.last_triggered_at""", updates,
                )
        return triggered
