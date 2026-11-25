"""在S03的22~23点窗口, 以已保存五维归档幂等替换固定自选组。

本模块只同步成员关系与回执, 不评分、不改变策略股票范围或发布业务事件。
"""
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
    """外层可重试的同步失败, 不得清空原组或冒充评分成功。"""
    pass


def _marker(storage, value=None):
    """同步摘要沿租户文档或文件作业存储保存, None表示读取。"""
    if isinstance(storage, TenantDocuments):
        return (storage.get(NAMESPACE, S03_ID, {}) if value is None
                else storage.put(NAMESPACE, S03_ID, value))
    return DailySelections(storage).job(NAMESPACE, value)


def _group(groups):
    """固定组名称必须唯一, 沿用现有ID; 缺组不自动新建替代组。"""
    matches = [g for g in groups if g["name"].strip().casefold() == "s03"]
    if len(matches) != 1:
        raise ValueError("固定分组s03不存在或名称不唯一")
    return matches[0]["id"]


def _sql(storage, snapshot, symbols, marker):
    """租户事务和自选锁内替换关系, 摘要相同不写, 较旧日期拒绝。

    独占旧成员删除, 共享成员保留其他组/备注/策略关系; 新名单顺序、
    marker与revision同事务写入。异常交外层回滚, 不提前发刷新事件。
    """
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
        # 共享成员保留备注及其他组/策略关系, 只摘本组旧关系。
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
    """自选锁内替换文件并保存marker, 失败恢复原文件与内存revision。

    成功评分归档独立保留; 外层窗口内只重试本同步, 不重新计算五维。
    """
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
    """校验窗口、精确日期、已保存对象和证券列表后同步固定组。

    真实空名单允许清组; 缺归档/身份、重复或超过30只、窗口外均不清组。
    返回同步回执或幂等None; 异常包装成可重试错误, 调用方提交后发布事件。
    """
    if not snapshot or snapshot.get("scoring_policy") != POLICY:
        return None
    try:
        now = as_cn_datetime(now)
        window = due_window(now, monitored_only=True, strategy_id=S03_ID)
        if (window is None or (str(window[0]), str(window[1])) !=
                (snapshot.get("source_date"), snapshot.get("as_of"))):
            raise ValueError("不在该归档的22:00至23:00交易日窗口")
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
