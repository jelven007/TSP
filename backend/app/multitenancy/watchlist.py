"""Original watchlist contract backed by explicit tenant SQL transactions."""
from __future__ import annotations

import uuid
from collections.abc import Callable

from psycopg.errors import UniqueViolation

from app.cell.context import TenantContext
from app.cell.database import Databases
from app.cell.watchlist import Watchlist as CellWatchlist
from app.market_scope import removed_market_symbol
from app.services.watchlist import _normalize_group_color, _normalize_group_name


class Watchlist:
    def __init__(self, db: Databases, context: TenantContext):
        self.db = db
        self.context = context

    @staticmethod
    def _rows(conn) -> list[dict]:
        rows = conn.execute(
            """SELECT i.symbol,i.note,i.added_at,
                      coalesce(array_agg(g.group_id ORDER BY g.group_id)
                               FILTER (WHERE g.group_id IS NOT NULL),'{}') AS group_ids
               FROM business.watchlist_items i
               LEFT JOIN business.watchlist_group_items g
                 ON g.tenant_id=i.tenant_id AND g.symbol=i.symbol
               GROUP BY i.tenant_id,i.symbol
               ORDER BY i.position,i.added_at DESC,i.symbol"""
        ).fetchall()
        return [{**row, "added_at": row["added_at"].isoformat()} for row in rows]

    def _mutate(self, action: str, target: str, operation: Callable):
        with self.db.business_transaction(self.context, write=True) as conn:
            CellWatchlist._lock(conn)
            result = operation(conn)
            conn.execute(
                """INSERT INTO business.documents(tenant_id,namespace,id,value)
                   VALUES(%s,'revision','watchlist','{}')
                   ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET
                     revision=business.documents.revision+1,updated_at=now()""", (self.context.tenant_id,),
            )
            CellWatchlist._audit(conn, self.context, action, target)
            return result

    def revision(self) -> int:
        with self.db.business_transaction(self.context) as conn:
            row = conn.execute(
                "SELECT revision FROM business.documents WHERE namespace='revision' AND id='watchlist'"
            ).fetchone()
            return row["revision"] if row else 0

    def list_symbols(self) -> list[dict]:
        with self.db.business_transaction(self.context) as conn:
            return self._rows(conn)

    def list_groups(self) -> list[dict]:
        with self.db.business_transaction(self.context) as conn:
            return CellWatchlist._groups(conn)

    @staticmethod
    def _require_group(conn, group_id: str | None):
        if group_id is not None and not conn.execute(
            "SELECT 1 FROM business.watchlist_groups WHERE id=%s", (group_id,),
        ).fetchone():
            raise ValueError("自选分组不存在")

    @staticmethod
    def _require_symbol(conn, symbol: str):
        if not conn.execute(
            "SELECT 1 FROM business.watchlist_items WHERE symbol=%s", (symbol,),
        ).fetchone():
            raise KeyError(symbol)

    def add(self, symbol: str, note: str = "", group_id: str | None = None) -> list[dict]:
        return self.add_batch([symbol], note, group_id=group_id)[0]

    def add_batch(
        self, symbols: list[str], note: str = "", group_id: str | None = None,
        group_ids: list[str] | None = None, *, only_new: bool = False,
    ) -> tuple[list[dict], int]:
        if len(symbols) > 5000 or len(note) > 4096:
            raise ValueError("自选批量或备注超出限制")
        symbols = [symbol for symbol in symbols if not removed_market_symbol(symbol)]
        if any(not isinstance(symbol, str) or not 1 <= len(symbol) <= 32 for symbol in symbols):
            raise ValueError("证券代码格式错误")
        groups = list(dict.fromkeys([*(group_ids or []), *([group_id] if group_id else [])]))

        def apply(conn):
            for group in groups:
                self._require_group(conn, group)
            existing = {row["symbol"] for row in self._rows(conn)}
            added = len(set(symbols) - existing)
            for symbol in symbols:
                if only_new and symbol in existing:
                    continue
                conn.execute(
                    """INSERT INTO business.watchlist_items(tenant_id,symbol,note,added_at,position)
                       SELECT %s,%s,%s,clock_timestamp(),coalesce(min(position),0)-1
                       FROM business.watchlist_items
                       ON CONFLICT(tenant_id,symbol) DO UPDATE SET
                         note=excluded.note,added_at=excluded.added_at,position=excluded.position""",
                    (self.context.tenant_id, symbol, note),
                )
                existing.add(symbol)
                for group in groups:
                    conn.execute(
                        """INSERT INTO business.watchlist_group_items(tenant_id,group_id,symbol)
                           VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""", (self.context.tenant_id, group, symbol),
                    )
            return self._rows(conn), added

        return self._mutate("watchlist.add", ",".join(symbols)[:4096], apply)

    def remove(self, symbol: str) -> list[dict]:
        def apply(conn):
            conn.execute("DELETE FROM business.watchlist_items WHERE symbol=%s", (symbol,))
            return self._rows(conn)
        return self._mutate("watchlist.remove", symbol, apply)

    def clear(self) -> int:
        return self._mutate(
            "watchlist.clear", "*", lambda conn: conn.execute("DELETE FROM business.watchlist_items").rowcount,
        )

    def move_to_top(self, symbol: str) -> list[dict]:
        def apply(conn):
            conn.execute(
                """UPDATE business.watchlist_items SET
                   position=(SELECT coalesce(min(position),0)-1 FROM business.watchlist_items)
                   WHERE symbol=%s""", (symbol,),
            )
            return self._rows(conn)
        return self._mutate("watchlist.top", symbol, apply)

    def create_group(self, name: str, color: str | None = None) -> tuple[list[dict], dict]:
        group = {"id": uuid.uuid4().hex, "name": _normalize_group_name(name),
                 "color": _normalize_group_color(color)}

        def apply(conn):
            conn.execute(
                """INSERT INTO business.watchlist_groups(tenant_id,id,name,color,position)
                   SELECT %s,%s,%s,%s,coalesce(max(position),-1)+1 FROM business.watchlist_groups""",
                (self.context.tenant_id, group["id"], group["name"], group["color"]),
            )
            return CellWatchlist._groups(conn), group
        try:
            return self._mutate("watchlist.group.create", group["id"], apply)
        except UniqueViolation as exc:
            raise ValueError("分组名称已存在") from exc

    def rename_group(self, group_id: str, name: str, color: str | None = None) -> list[dict]:
        name = _normalize_group_name(name)
        color = _normalize_group_color(color) if color is not None else None

        def apply(conn):
            result = conn.execute(
                "UPDATE business.watchlist_groups SET name=%s,color=coalesce(%s,color) WHERE id=%s",
                (name, color, group_id),
            )
            if not result.rowcount:
                raise KeyError(group_id)
            return CellWatchlist._groups(conn)
        try:
            return self._mutate("watchlist.group.rename", group_id, apply)
        except UniqueViolation as exc:
            raise ValueError("分组名称已存在") from exc

    def reorder_groups(self, ordered_ids: list[str]) -> list[dict]:
        def apply(conn):
            existing = {row["id"] for row in CellWatchlist._groups(conn)}
            if len(ordered_ids) != len(existing) or set(ordered_ids) != existing:
                raise ValueError("分组顺序与现有分组不一致")
            for position, group in enumerate(ordered_ids):
                conn.execute("UPDATE business.watchlist_groups SET position=%s WHERE id=%s", (position, group))
            return CellWatchlist._groups(conn)
        return self._mutate("watchlist.groups.reorder", ",".join(ordered_ids), apply)

    def delete_group(self, group_id: str) -> tuple[list[dict], list[dict]]:
        def apply(conn):
            if not conn.execute("DELETE FROM business.watchlist_groups WHERE id=%s", (group_id,)).rowcount:
                raise KeyError(group_id)
            return CellWatchlist._groups(conn), self._rows(conn)
        return self._mutate("watchlist.group.delete", group_id, apply)

    def clear_group(self, group_id: str) -> list[dict]:
        def apply(conn):
            if not conn.execute("SELECT 1 FROM business.watchlist_groups WHERE id=%s", (group_id,)).fetchone():
                raise KeyError(group_id)
            conn.execute("DELETE FROM business.watchlist_group_items WHERE group_id=%s", (group_id,))
            return self._rows(conn)
        return self._mutate("watchlist.group.clear", group_id, apply)

    def set_group(self, symbol: str, group_id: str | None) -> list[dict]:
        def apply(conn):
            self._require_symbol(conn, symbol)
            self._require_group(conn, group_id)
            conn.execute("DELETE FROM business.watchlist_group_items WHERE symbol=%s", (symbol,))
            if group_id is not None:
                conn.execute(
                    "INSERT INTO business.watchlist_group_items(tenant_id,group_id,symbol) VALUES(%s,%s,%s)",
                    (self.context.tenant_id, group_id, symbol),
                )
            return self._rows(conn)
        return self._mutate("watchlist.group.assign", symbol, apply)

    def add_to_group(self, symbol: str, group_id: str) -> list[dict]:
        def apply(conn):
            self._require_symbol(conn, symbol)
            self._require_group(conn, group_id)
            conn.execute(
                """INSERT INTO business.watchlist_group_items(tenant_id,group_id,symbol)
                   VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""", (self.context.tenant_id, group_id, symbol),
            )
            return self._rows(conn)
        return self._mutate("watchlist.group.add-member", symbol, apply)

    def remove_from_group(self, symbol: str, group_id: str) -> list[dict]:
        def apply(conn):
            self._require_symbol(conn, symbol)
            self._require_group(conn, group_id)
            conn.execute(
                "DELETE FROM business.watchlist_group_items WHERE group_id=%s AND symbol=%s", (group_id, symbol),
            )
            return self._rows(conn)
        return self._mutate("watchlist.group.remove-member", symbol, apply)
