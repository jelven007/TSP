from __future__ import annotations

import uuid

from psycopg.errors import UniqueViolation

from app.cell.context import AccessError, TenantContext
from app.cell.database import Databases


class Watchlist:
    def __init__(self, db: Databases):
        self.db = db

    @staticmethod
    def _rows(conn) -> list[dict]:
        # tenant predicates are deliberately supplied by RLS.
        return conn.execute(
            """SELECT i.symbol, i.note, i.added_at,
                      coalesce(array_agg(g.group_id ORDER BY g.group_id)
                               FILTER (WHERE g.group_id IS NOT NULL), '{}') AS group_ids
               FROM business.watchlist_items i
               LEFT JOIN business.watchlist_group_items g
                 ON g.tenant_id = i.tenant_id AND g.symbol = i.symbol
               GROUP BY i.tenant_id, i.symbol ORDER BY i.added_at DESC, i.symbol"""
        ).fetchall()

    @staticmethod
    def _groups(conn) -> list[dict]:
        return conn.execute(
            "SELECT id, name, color FROM business.watchlist_groups ORDER BY position, id"
        ).fetchall()

    @staticmethod
    def _audit(conn, context: TenantContext, action: str, target: str) -> None:
        conn.execute(
            """INSERT INTO business.audit(tenant_id, id, actor_id, action, target_id, request_id)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (context.tenant_id, uuid.uuid4().hex, context.user_id, action, target, context.request_id),
        )

    @staticmethod
    def _lock(conn) -> None:
        # Serialize only this tenant's mutations, including group deletion vs item assignment.
        conn.execute("SELECT tenant_id FROM business.tenant_bindings FOR UPDATE")

    def list(self, context: TenantContext) -> dict:
        context.require("watchlist:read")
        with self.db.business_transaction(context) as conn:
            return {"symbols": self._rows(conn), "groups": self._groups(conn)}

    def add(self, context: TenantContext, symbols: list[str], note: str, groups: list[str]) -> dict:
        context.require("watchlist:write")
        with self.db.business_transaction(context, write=True) as conn:
            self._lock(conn)
            available = {row["id"] for row in self._groups(conn)}
            if not set(groups) <= available:
                raise AccessError(404, "group_not_found")
            for symbol in dict.fromkeys(symbols):
                conn.execute(
                    """INSERT INTO business.watchlist_items(tenant_id, symbol, note)
                       VALUES (%s, %s, %s) ON CONFLICT (tenant_id, symbol)
                       DO UPDATE SET note = excluded.note, added_at = clock_timestamp()""",
                    (context.tenant_id, symbol, note),
                )
                for group in dict.fromkeys(groups):
                    conn.execute(
                        """INSERT INTO business.watchlist_group_items(tenant_id, group_id, symbol)
                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                        (context.tenant_id, group, symbol),
                    )
            self._audit(conn, context, "watchlist.add", ",".join(dict.fromkeys(symbols)))
            return {"symbols": self._rows(conn)}

    def remove(self, context: TenantContext, symbol: str | None) -> dict:
        context.require("watchlist:write")
        with self.db.business_transaction(context, write=True) as conn:
            self._lock(conn)
            if symbol is None:
                conn.execute("DELETE FROM business.watchlist_items")
            else:
                conn.execute("DELETE FROM business.watchlist_items WHERE symbol = %s", (symbol,))
            self._audit(conn, context, "watchlist.clear" if symbol is None else "watchlist.remove", symbol or "*")
            return {"symbols": self._rows(conn)}

    def group(self, context: TenantContext, group_id: str | None, name: str, color: str) -> dict:
        context.require("watchlist:write")
        group_id = group_id or uuid.uuid4().hex
        try:
            with self.db.business_transaction(context, write=True) as conn:
                self._lock(conn)
                exists = conn.execute(
                    "SELECT 1 FROM business.watchlist_groups WHERE id = %s", (group_id,)
                ).fetchone()
                if exists:
                    conn.execute(
                        "UPDATE business.watchlist_groups SET name = %s, color = %s WHERE id = %s",
                        (name, color, group_id),
                    )
                else:
                    conn.execute(
                        """INSERT INTO business.watchlist_groups(tenant_id, id, name, color, position)
                           SELECT %s, %s, %s, %s, coalesce(max(position), -1) + 1
                           FROM business.watchlist_groups""",
                        (context.tenant_id, group_id, name, color),
                    )
                self._audit(conn, context, "watchlist.group.set", group_id)
                return {"group_id": group_id, "groups": self._groups(conn)}
        except UniqueViolation as exc:
            raise AccessError(409, "group_name_exists") from exc

    def delete_group(self, context: TenantContext, group_id: str) -> dict:
        context.require("watchlist:write")
        with self.db.business_transaction(context, write=True) as conn:
            self._lock(conn)
            result = conn.execute("DELETE FROM business.watchlist_groups WHERE id = %s", (group_id,))
            if not result.rowcount:
                raise AccessError(404, "group_not_found")
            self._audit(conn, context, "watchlist.group.remove", group_id)
            return {"groups": self._groups(conn), "symbols": self._rows(conn)}

    def reorder(self, context: TenantContext, ordered_ids: list[str]) -> dict:
        context.require("watchlist:write")
        with self.db.business_transaction(context, write=True) as conn:
            self._lock(conn)
            existing = {row["id"] for row in self._groups(conn)}
            if len(ordered_ids) != len(existing) or set(ordered_ids) != existing:
                raise AccessError(422, "invalid_group_order")
            for position, group_id in enumerate(ordered_ids):
                conn.execute(
                    "UPDATE business.watchlist_groups SET position = %s WHERE id = %s",
                    (position, group_id),
                )
            self._audit(conn, context, "watchlist.groups.reorder", ",".join(ordered_ids))
            return {"groups": self._groups(conn)}

    def assign(self, context: TenantContext, symbol: str, group_ids: list[str]) -> dict:
        context.require("watchlist:write")
        with self.db.business_transaction(context, write=True) as conn:
            self._lock(conn)
            if not conn.execute("SELECT 1 FROM business.watchlist_items WHERE symbol = %s", (symbol,)).fetchone():
                raise AccessError(404, "symbol_not_found")
            if not set(group_ids) <= {row["id"] for row in self._groups(conn)}:
                raise AccessError(404, "group_not_found")
            conn.execute("DELETE FROM business.watchlist_group_items WHERE symbol = %s", (symbol,))
            for group_id in dict.fromkeys(group_ids):
                conn.execute(
                    "INSERT INTO business.watchlist_group_items(tenant_id, group_id, symbol) VALUES (%s, %s, %s)",
                    (context.tenant_id, group_id, symbol),
                )
            self._audit(conn, context, "watchlist.groups.assign", symbol)
            return {"symbols": self._rows(conn)}
