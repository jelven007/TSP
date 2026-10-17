"""Transactional storage adapter for the original paper-trading domain."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments, json_value

MAX_ACCOUNTS = 100
MAX_ORDERS = 100000


@dataclass(frozen=True, slots=True)
class TenantPaper(TenantDocuments):
    public_data_dir: Path


@dataclass(slots=True)
class PaperSession:
    """One already-authorized transaction; never a filesystem path."""

    documents: TenantDocuments
    conn: Any
    writable: bool

    @property
    def tenant_id(self):
        return self.documents.context.tenant_id

    def account(self, account_id):
        row = self.conn.execute(
            "SELECT value FROM business.full_paper_accounts WHERE id=%s", (account_id,),
        ).fetchone()
        return row["value"] if row else None

    def save_account(self, account_id, value):
        if self.account(account_id) is None:
            count = self.conn.execute("SELECT count(*) AS n FROM business.full_paper_accounts").fetchone()["n"]
            if count >= MAX_ACCOUNTS:
                raise AccessError(429, "模拟账户数量已达上限")
        self.conn.execute(
            """INSERT INTO business.full_paper_accounts(tenant_id,id,value) VALUES(%s,%s,%s)
               ON CONFLICT(tenant_id,id) DO UPDATE SET
               value=excluded.value,revision=business.full_paper_accounts.revision+1""",
            (self.tenant_id, account_id, Jsonb(json_value(value))),
        )

    def account_ids(self):
        return [row["id"] for row in self.conn.execute(
            "SELECT id FROM business.full_paper_accounts ORDER BY value->>'created_at',id",
        ).fetchall()]

    def order(self, account_id, order_id):
        row = self.conn.execute(
            "SELECT value FROM business.full_paper_orders WHERE account_id=%s AND id=%s",
            (account_id, order_id),
        ).fetchone()
        return row["value"] if row else None

    def orders(self, account_id):
        return [row["value"] for row in self.conn.execute(
            "SELECT value FROM business.full_paper_orders WHERE account_id=%s ORDER BY id",
            (account_id,),
        ).fetchall()]

    def order_page(self, account_id, *, status=None, limit=100, before=None):
        created_at, order_id = before or (None, None)
        total = self.conn.execute(
            """SELECT count(*) AS n FROM business.full_paper_orders
               WHERE account_id=%s AND (%s::text IS NULL OR value->>'status'=%s)""",
            (account_id, status, status),
        ).fetchone()["n"]
        rows = self.conn.execute(
            """SELECT value,coalesce(value->>'created_at','') AS created_key,id
               FROM business.full_paper_orders
               WHERE account_id=%s AND (%s::text IS NULL OR value->>'status'=%s)
                 AND (%s::text IS NULL OR
                      (coalesce(value->>'created_at',''),id)<(%s,%s))
               ORDER BY coalesce(value->>'created_at','') DESC,id DESC LIMIT %s""",
            (account_id, status, status, created_at, created_at, order_id, limit + 1),
        ).fetchall()
        page = rows[:limit]
        next_key = (page[-1]["created_key"], page[-1]["id"]) if len(rows) > limit else None
        return [row["value"] for row in page], total, next_key

    def save_order(self, account_id, value):
        if self.order(account_id, value["id"]) is None:
            count = self.conn.execute(
                "SELECT count(*) AS n FROM business.full_paper_orders WHERE account_id=%s", (account_id,),
            ).fetchone()["n"]
            if count >= MAX_ORDERS:
                raise AccessError(429, "模拟订单数量已达上限")
        self.conn.execute(
            """INSERT INTO business.full_paper_orders(tenant_id,account_id,id,value)
               VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,account_id,id) DO UPDATE SET value=excluded.value""",
            (self.tenant_id, account_id, value["id"], Jsonb(json_value(value))),
        )

    def fills(self, account_id):
        return [row["value"] for row in self.conn.execute(
            "SELECT value FROM business.full_paper_fills WHERE account_id=%s ORDER BY ordinal",
            (account_id,),
        ).fetchall()]

    def fill_page(self, account_id, *, limit=100, before=None):
        counts = self.conn.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE coalesce(value->>'kind','fill')='fill')
                          AS execution_total
               FROM business.full_paper_fills WHERE account_id=%s""",
            (account_id,),
        ).fetchone()
        rows = self.conn.execute(
            """SELECT value,ordinal FROM business.full_paper_fills
               WHERE account_id=%s AND (%s::bigint IS NULL OR ordinal<%s)
               ORDER BY ordinal DESC LIMIT %s""",
            (account_id, before, before, limit + 1),
        ).fetchall()
        page = rows[:limit]
        next_key = page[-1]["ordinal"] if len(rows) > limit else None
        return (
            [row["value"] for row in page],
            counts["total"],
            counts["execution_total"],
            next_key,
        )

    def append_fill(self, account_id, value):
        key = (
            f"action:{value['action_id']}:{value['stage']}" if value.get("kind") == "corporate_action"
            else f"corp:{value['symbol']}:{value['date']}" if value.get("kind") == "corp_action"
            else f"fill:{value['order_id']}"
        )
        existing = self.conn.execute(
            "SELECT value FROM business.full_paper_fills WHERE account_id=%s AND id=%s",
            (account_id, key),
        ).fetchone()
        if existing:
            if existing["value"] != json_value(value):
                raise AccessError(409, "重复成交内容不一致")
            return
        # Account mutations hold the tenant binding lock across the complete
        # original-domain operation. The ordinal records replay order even
        # when several fills have the same millisecond timestamp.
        self.conn.execute(
            """INSERT INTO business.full_paper_fills(tenant_id,account_id,id,ordinal,value)
               SELECT %s,%s,%s,coalesce(max(ordinal),0)+1,%s
               FROM business.full_paper_fills WHERE account_id=%s""",
            (self.tenant_id, account_id, key, Jsonb(json_value(value)), account_id),
        )

    def positions(self, account_id):
        row = self.conn.execute(
            "SELECT positions FROM business.full_paper_accounts WHERE id=%s", (account_id,),
        ).fetchone()
        return row["positions"] if row else None

    def save_positions(self, account_id, positions):
        self.conn.execute(
            "UPDATE business.full_paper_accounts SET positions=%s WHERE id=%s",
            (Jsonb(json_value(positions)), account_id),
        )

    def nav(self, account_id):
        return [row["value"] for row in self.conn.execute(
            "SELECT value FROM business.full_paper_nav WHERE account_id=%s ORDER BY day",
            (account_id,),
        ).fetchall()]

    def nav_page(self, account_id, *, limit=500, before=None):
        total = self.conn.execute(
            "SELECT count(*) AS n FROM business.full_paper_nav WHERE account_id=%s",
            (account_id,),
        ).fetchone()["n"]
        rows = self.conn.execute(
            """SELECT value,day FROM business.full_paper_nav
               WHERE account_id=%s AND (%s::date IS NULL OR day<%s::date)
               ORDER BY day DESC LIMIT %s""",
            (account_id, before, before, limit + 1),
        ).fetchall()
        page = rows[:limit]
        next_key = page[-1]["day"].isoformat() if len(rows) > limit else None
        return [row["value"] for row in reversed(page)], total, next_key

    def save_nav(self, account_id, day, nav):
        self.conn.execute(
            """INSERT INTO business.full_paper_nav(tenant_id,account_id,day,value) VALUES(%s,%s,%s,%s)
               ON CONFLICT(tenant_id,account_id,day) DO UPDATE SET value=excluded.value""",
            (self.tenant_id, account_id, day, Jsonb(json_value(nav))),
        )

    def idempotent_order(self, account_id, key, inputs):
        if key is None:
            return None, None
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError("订单请求标识必须是 1 到 128 字符")
        digest = hashlib.sha256(json.dumps(json_value(inputs), sort_keys=True).encode()).hexdigest()
        row = self.conn.execute(
            """SELECT input_hash,value FROM business.full_paper_orders
               WHERE account_id=%s AND idempotency_key=%s""", (account_id, key),
        ).fetchone()
        if row and row["input_hash"] != digest:
            raise AccessError(409, "相同订单请求标识的参数不一致")
        return row["value"] if row else None, digest

    def bind_order_request(self, account_id, order_id, key, digest):
        if key is not None:
            self.conn.execute(
                """UPDATE business.full_paper_orders SET idempotency_key=%s,input_hash=%s
                   WHERE account_id=%s AND id=%s""", (key, digest, account_id, order_id),
            )

    def document(self, namespace, item_id):
        row = self.conn.execute(
            "SELECT value FROM business.documents WHERE namespace=%s AND id=%s", (namespace, item_id),
        ).fetchone()
        return row["value"] if row else None

    def document_values(self, namespace):
        return [row["value"] for row in self.conn.execute(
            "SELECT value FROM business.documents WHERE namespace=%s ORDER BY id", (namespace,),
        ).fetchall()]

    def put_document(self, namespace, item_id, value):
        self.conn.execute(
            """INSERT INTO business.documents(tenant_id,namespace,id,value) VALUES(%s,%s,%s,%s)
               ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET value=excluded.value,
               revision=business.documents.revision+1,updated_at=now()""",
            (self.tenant_id, namespace, item_id, Jsonb(json_value(value))),
        )

    def delete_document(self, namespace, item_id):
        return self.conn.execute(
            "DELETE FROM business.documents WHERE namespace=%s AND id=%s", (namespace, item_id),
        ).rowcount > 0

    def alert(self, event):
        """Commit the fill's alert together with its ledger entry."""
        from app.multitenancy.alerts import append_in_transaction

        append_in_transaction(self.conn, self.tenant_id, [event])


def paper_operation(*, write=False):
    """Run the unmodified domain computation in a single SQL transaction."""
    def decorate(function):
        @wraps(function)
        def run(data_dir, *args, **kwargs):
            if isinstance(data_dir, PaperSession):
                if write and not data_dir.writable:
                    raise RuntimeError("paper mutation attempted in a read transaction")
                return function(data_dir, *args, **kwargs)
            if not isinstance(data_dir, TenantDocuments):
                return function(data_dir, *args, **kwargs)
            with data_dir.db.business_transaction(
                data_dir.context, write=write, permission="paper:write" if write else "paper:read",
            ) as conn:
                return function(PaperSession(data_dir, conn, write), *args, **kwargs)
        return run
    return decorate


@contextmanager
def paper_transaction(data_dir):
    if not isinstance(data_dir, TenantDocuments):
        yield data_dir
        return
    with data_dir.db.business_transaction(data_dir.context, write=True, permission="paper:write") as conn:
        yield PaperSession(data_dir, conn, True)


def paper_lock(data_dir, legacy_lock):
    return nullcontext() if isinstance(data_dir, PaperSession) else legacy_lock


def market_path(data_dir) -> Path:
    if isinstance(data_dir, PaperSession):
        data_dir = data_dir.documents
    if isinstance(data_dir, TenantPaper):
        return data_dir.public_data_dir
    if isinstance(data_dir, TenantDocuments):
        raise RuntimeError("paper market reads require an explicit public data directory")
    return data_dir
