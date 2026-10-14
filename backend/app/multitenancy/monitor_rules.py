"""Original monitor definitions with tenant SQL versions and atomic related writes."""
from psycopg.types.json import Jsonb

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments, json_value

MAX_RULES = 200


class Rules:
    def __init__(self, documents: TenantDocuments):
        self.documents = documents

    def load_all(self):
        with self.documents._transaction() as conn:
            return [row["definition"] for row in conn.execute(
                "SELECT definition FROM business.monitor_rules ORDER BY id",
            ).fetchall()]

    def load_one(self, rule_id):
        with self.documents._transaction() as conn:
            row = conn.execute(
                "SELECT definition FROM business.monitor_rules WHERE id=%s", (rule_id,),
            ).fetchone()
            return row["definition"] if row else None

    def save_one(self, rule):
        with self.documents.db.control.connection() as control:
            user = control.execute(
                "SELECT auth_version,active,account_state FROM control.users WHERE id=%s",
                (self.documents.context.user_id,),
            ).fetchone()
        if not user or not user["active"] or user["account_state"] != "active":
            raise AccessError(403, "监控账户不可用")
        rule = {**rule, "_execution": {
            "user_id": self.documents.context.user_id, "auth_version": user["auth_version"],
            "policy_version": self.documents.context.policy_version,
            "routing_epoch": self.documents.context.routing_epoch,
        }}
        with self.documents._transaction(write=True) as conn:
            selected = set(rule.get("webhook_channels") or [])
            available = {row["id"] for row in conn.execute(
                "SELECT id FROM business.notification_channels WHERE id=ANY(%s)", (list(selected),),
            ).fetchall()}
            if not selected.issubset(available):
                raise AccessError(400, "通知渠道不存在")
            existing = conn.execute(
                "SELECT definition FROM business.monitor_rules WHERE id=%s", (rule["id"],),
            ).fetchone()
            if existing:
                previous = existing["definition"]
                if previous.get("lot_id") and previous["lot_id"] != rule.get("lot_id"):
                    raise AccessError(409, "lot_rule_managed")
                rule = {**rule, "created_at": previous.get("created_at", rule["created_at"])}
            if rule.get("scope") == "watchlist_group" and not conn.execute(
                "SELECT 1 FROM business.watchlist_groups WHERE id=%s", (rule.get("group_id"),),
            ).fetchone():
                raise AccessError(400, "watchlist_group_missing")
            if not existing and conn.execute("SELECT count(*) AS n FROM business.monitor_rules").fetchone()["n"] >= MAX_RULES:
                raise AccessError(429, "monitor_rule_limit")
            conn.execute(
                """INSERT INTO business.monitor_rules(tenant_id,id,definition,enabled) VALUES(%s,%s,%s,%s)
                   ON CONFLICT(tenant_id,id) DO UPDATE SET definition=excluded.definition,
                   enabled=excluded.enabled,version=business.monitor_rules.version+1""",
                (self.documents.context.tenant_id, rule["id"], Jsonb(json_value(rule)), rule.get("enabled", True)),
            )

    def delete_one(self, rule_id, *, managed=False):
        with self.documents._transaction(write=True) as conn:
            row = conn.execute("SELECT definition FROM business.monitor_rules WHERE id=%s", (rule_id,)).fetchone()
            if row and row["definition"].get("lot_id") and not managed:
                raise AccessError(409, "lot_rule_managed")
            return conn.execute("DELETE FROM business.monitor_rules WHERE id=%s", (rule_id,)).rowcount > 0
