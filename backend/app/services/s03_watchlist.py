"""Replace the fixed S03 group from its saved nightly selection only."""
from __future__ import annotations

import os
import re

from app.market_time import as_cn_datetime
from app.multitenancy.documents import TenantDocuments
from app.services.daily_selections import DailySelections, due_window
from app.services.selection_clock import S03_ID
from app.strategy.version import digest

NAMESPACE = "s03_watchlist_sync"
POLICY = "s03_five_dimensions_v1"


class WatchlistSyncError(RuntimeError):
    pass


def _marker(storage, value=None):
    if isinstance(storage, TenantDocuments):
        return (storage.get(NAMESPACE, S03_ID, {}) if value is None
                else storage.put(NAMESPACE, S03_ID, value))
    return DailySelections(storage).job(NAMESPACE, value)


def _group(groups):
    matches = [g for g in groups if g["name"].strip().casefold() == "s03"]
    if len(matches) != 1:
        raise ValueError("固定分组s03不存在或名称不唯一")
    return matches[0]["id"]


def _sql(storage, snapshot, symbols, marker):
    from app.cell.watchlist import Watchlist as CellWatchlist
    from app.multitenancy.watchlist import Watchlist
    with storage.transaction() as unit:
        conn = unit.connection
        CellWatchlist._lock(conn)
        previous = _marker(unit)
        if previous.get("archive_hash") == marker["archive_hash"]:
            return None
        if previous.get("as_of", "") > snapshot["as_of"]:
            raise ValueError("较老归档不能覆盖最新分组")
        gid = _group(CellWatchlist._groups(conn))
        rows = Watchlist._rows(conn)
        old = [r for r in rows if gid in r["group_ids"]]
        exclusive = [r["symbol"] for r in old if r["group_ids"] == [gid]]
        conn.execute("DELETE FROM business.documents WHERE namespace='watchlist_strategies' AND id=ANY(%s)",
                     (exclusive,))
        conn.execute("DELETE FROM business.watchlist_items WHERE symbol=ANY(%s)", (exclusive,))
        conn.execute("DELETE FROM business.watchlist_group_items WHERE group_id=%s", (gid,))
        # Preserve notes and other group/strategy relations for shared members.
        for symbol in reversed(symbols):
            conn.execute(
                """INSERT INTO business.watchlist_items(tenant_id,symbol,note,added_at,position)
                   SELECT %s,%s,'',clock_timestamp(),coalesce(min(position),0)-1
                   FROM business.watchlist_items
                   ON CONFLICT(tenant_id,symbol) DO UPDATE SET position=excluded.position""",
                (unit.context.tenant_id, symbol))
            conn.execute(
                """INSERT INTO business.watchlist_group_items(tenant_id,group_id,symbol)
                   VALUES(%s,%s,%s)""", (unit.context.tenant_id, gid, symbol))
        receipt = {**marker, "group_id": gid, "removed_total": len(old),
                   "deleted_total": len(exclusive), "selected_total": len(symbols)}
        _marker(unit, receipt)
        conn.execute(
            """INSERT INTO business.documents(tenant_id,namespace,id,value)
               VALUES(%s,'revision','watchlist','{}')
               ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET
                 revision=business.documents.revision+1,updated_at=now()""", (unit.context.tenant_id,))
        CellWatchlist._audit(conn, unit.context, "watchlist.s03.replace", gid)
        return receipt


def _file(storage, snapshot, symbols, marker):
    import polars as pl

    from app.services import watchlist
    with watchlist._LOCK:
        previous = _marker(storage)
        if previous.get("archive_hash") == marker["archive_hash"]:
            return None
        if previous.get("as_of", "") > snapshot["as_of"]:
            raise ValueError("较老归档不能覆盖最新分组")
        gid = _group(watchlist._read_groups())
        rows, old, deleted = [], 0, 0
        for row in watchlist._read_entries().to_dicts():
            if gid in row["group_ids"]:
                old += 1
                row["group_ids"] = [g for g in row["group_ids"] if g != gid]
                if not row["group_ids"]:
                    deleted += 1
                    continue
            rows.append(row)
        by_symbol = {r["symbol"]: r for r in rows}
        selected = []
        for symbol in symbols:
            row = by_symbol.get(symbol, {"symbol": symbol, "note": "", "group_ids": [],
                                         "strategy_ids": [], "added_at": marker["synced_at"]})
            row["group_ids"] = [*row["group_ids"], gid]
            selected.append(row)
        out = [*selected, *(r for r in rows if r["symbol"] not in symbols)]
        receipt = {**marker, "group_id": gid, "removed_total": old,
                   "deleted_total": deleted, "selected_total": len(symbols)}
        path = watchlist._path()
        before = path.read_bytes() if path.exists() else None
        revision = watchlist.revision()
        try:
            watchlist._write_entries(pl.DataFrame(out, schema=watchlist._ENTRY_SCHEMA))
            _marker(storage, receipt)
        except Exception:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                tmp = path.with_suffix(".restore.tmp")
                tmp.write_bytes(before)
                os.replace(tmp, path)
            watchlist._REVISION = revision
            raise
        return receipt


def sync_archived_selection(storage, snapshot, now):
    """Return a receipt only on replacement; callers publish after commit."""
    if not snapshot or snapshot.get("scoring_policy") != POLICY:
        return None
    try:
        now = as_cn_datetime(now)
        window = due_window(now, monitored_only=True, strategy_id=S03_ID)
        if (window is None or (str(window[0]), str(window[1])) !=
                (snapshot.get("source_date"), snapshot.get("as_of"))):
            raise ValueError("不在该归档的21:00至22:00交易日窗口")
        saved = DailySelections(storage).read(S03_ID, snapshot["as_of"])
        if saved != snapshot or not snapshot.get("identity"):
            raise ValueError("S03成功归档未保存或不匹配")
        rows = snapshot.get("rows")
        if not isinstance(rows, list) or len(rows) > 30:
            raise ValueError("S03预选名单格式或数量异常")
        symbols = [r.get("symbol") if isinstance(r, dict) else None for r in rows]
        if (any(not isinstance(s, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", s) for s in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError("S03预选证券代码缺失、重复或格式异常")
        marker = {"strategy_id": S03_ID, "source_date": snapshot["source_date"],
                  "as_of": snapshot["as_of"], "archive_hash": digest(snapshot),
                  "synced_at": now.isoformat()}
        return (_sql if isinstance(storage, TenantDocuments) else _file)(
            storage, snapshot, symbols, marker)
    except WatchlistSyncError:
        raise
    except Exception as exc:
        raise WatchlistSyncError(f"S03自选分组更新失败: {exc}") from exc
