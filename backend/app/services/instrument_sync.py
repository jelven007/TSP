"""标的维表同步服务。

盘前 9:10 获取沪深主板、科创板、创业板的全量有效股票元数据，
flatten ext 字段后原子替换 instruments.parquet。

Starter+ 盘后可用 quotes.get(universes) 顺便补充 name。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from app.data_providers.instrument_status import (
    CURRENT_A_SHARE_BOARDS,
    current_a_share_board,
    current_a_share_identity,
    is_delisted_name,
    normalize_instrument_name,
)
from app.instrument_history import update_instrument_history
from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ"]
_PERSISTENT_METADATA = ("listing_date", "total_shares", "float_shares", "tick_size")


class InstrumentSnapshotUnavailableError(RuntimeError):
    """No usable persisted current-stock snapshot exists."""


@dataclass(frozen=True)
class InstrumentPoolSnapshot:
    symbols: tuple[str, ...]
    as_of: date | None
    source: str
    boards: tuple[str, ...]


def load_current_stock_pool(
    data_dir: Path,
    *,
    require_complete: bool = False,
) -> InstrumentPoolSnapshot:
    """Load the last valid persisted four-board stock snapshot.

    Board classification is derived from the symbol so snapshots written by
    older versions remain usable.
    """
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    if not path.exists():
        raise InstrumentSnapshotUnavailableError(
            "没有可用的股票池快照；请先恢复股票目录数据源并完成一次标的同步"
        )
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:
        raise InstrumentSnapshotUnavailableError(f"股票池快照无法读取: {exc}") from exc

    rows: list[tuple[str, str]] = []
    for row in frame.iter_rows(named=True):
        identity = current_a_share_identity(
            row.get("symbol"),
            code=row.get("code"),
            exchange=row.get("exchange"),
        )
        if identity is None or is_delisted_name(row.get("name")):
            continue
        symbol, code, exchange = identity
        board = current_a_share_board(code=code, exchange=exchange)
        if board is not None:
            rows.append((symbol, board))
    if not rows:
        raise InstrumentSnapshotUnavailableError(
            "股票池快照不包含有效的沪主板、深主板、创业板或科创板股票"
        )

    as_of: date | None = None
    if "as_of" in frame.columns:
        values = frame["as_of"].drop_nulls().to_list()
        if values:
            latest = max(values)
            try:
                as_of = latest if isinstance(latest, date) else date.fromisoformat(str(latest)[:10])
            except ValueError:
                as_of = None
    source = "legacy"
    if "snapshot_source" in frame.columns:
        sources = {
            str(value) for value in frame["snapshot_source"].drop_nulls().to_list()
            if str(value)
        }
        if len(sources) == 1:
            source = sources.pop()

    unique = dict(rows)
    boards = set(unique.values())
    missing_boards = set(CURRENT_A_SHARE_BOARDS) - boards
    if require_complete and missing_boards:
        raise InstrumentSnapshotUnavailableError(
            "股票池快照板块不完整,缺少: " + ",".join(sorted(missing_boards))
        )
    return InstrumentPoolSnapshot(
        symbols=tuple(sorted(unique)),
        as_of=as_of,
        source=source,
        boards=tuple(sorted(boards)),
    )


def _metadata_value(field: str, value):
    if field == "listing_date":
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except (TypeError, ValueError):
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _preserve_metadata(
    rows: list[dict], previous: pl.DataFrame, *, as_of: date, source: str,
) -> list[dict]:
    """Keep last valid metadata with its original age; daily limits expire."""
    prior = {
        str(row["symbol"]): row for row in previous.iter_rows(named=True)
    } if "symbol" in previous.columns else {}
    for row in rows:
        old = prior.get(row["symbol"], {})
        row["name"] = row["name"] or old.get("name") or ""
        for field in _PERSISTENT_METADATA:
            fresh = _metadata_value(field, row.get(field))
            retained = _metadata_value(field, old.get(field))
            row[field] = fresh if fresh is not None else retained
            row[f"{field}_source"] = source if fresh is not None else (
                old.get(f"{field}_source") if retained is not None else None
            )
            row[f"{field}_as_of"] = as_of if fresh is not None else (
                old.get(f"{field}_as_of", old.get("as_of"))
                if retained is not None else None
            )
    return rows


def _flatten_instruments(items: list[dict]) -> list[dict]:
    """Flatten and normalize the current Shanghai/Shenzhen A-share pool."""
    rows = []
    for item in items:
        name = normalize_instrument_name(item.get("name"))
        identity = current_a_share_identity(
            item.get("symbol"),
            code=item.get("code"),
            exchange=item.get("exchange"),
        )
        if identity is None or is_delisted_name(name):
            continue
        symbol, code, exchange = identity
        row = {
            "symbol": symbol,
            "name": name,
            "code": code,
            "exchange": exchange,
            "board": current_a_share_board(code=code, exchange=exchange),
            "region": item.get("region") or "CN",
            "type": "stock",
        }
        ext = item.get("ext") or {}
        for field in ("listing_date", "total_shares", "float_shares", "tick_size",
                      "limit_up", "limit_down"):
            row[field] = ext.get(field, item.get(field))
        rows.append(row)
    return rows


def _fetch_instruments_via_provider(asset_type: str = "stock") -> list[dict] | None:
    """Follow the selected daily provider for all instrument catalogs.

    Only an explicit TickFlow selection returns None. An unavailable custom
    catalog returns [], so callers keep their last snapshot without mixing sources.
    """
    from app.services import preferences

    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        return None
    from app.data_providers import custom as custom_sources

    try:
        if not custom_sources.is_custom_provider(provider_name):
            logger.warning("instrument provider %s is unavailable", provider_name)
            return []
        provider = custom_sources.get_provider(provider_name)
        if not callable(getattr(provider, "get_instruments", None)):
            logger.warning("provider %s has no instrument catalog", provider_name)
            return []
        items = provider.get_instruments(asset_type)
        if isinstance(items, pl.DataFrame):
            items = items.to_dicts()
        rows = (
            _flatten_instruments(items or [])
            if asset_type == "stock"
            else [
                dict(item) for item in (items or [])
                if isinstance(item, dict)
                and item.get("symbol")
                and str(item["symbol"]).upper().endswith((".SH", ".SZ"))
                and not str(item["symbol"]).startswith(("4", "8", "92"))
            ]
        )
        metadata_fetch = getattr(provider, "get_instrument_metadata", None)
        if asset_type == "stock" and rows and callable(metadata_fetch):
            try:
                metadata = metadata_fetch([row["symbol"] for row in rows])
                if isinstance(metadata, pl.DataFrame):
                    metadata = metadata.to_dicts()
                by_symbol = {item["symbol"]: item for item in (metadata or [])}
                for row in rows:
                    extra = by_symbol.get(row["symbol"], {})
                    for field in _PERSISTENT_METADATA:
                        value = _metadata_value(field, extra.get(field))
                        if value is not None:
                            row[field] = value
            except Exception as exc:
                # Keep catalog availability and previously observed metadata.
                logger.warning("provider %s metadata enrichment failed: %s", provider_name, exc)
    except Exception as e:  # noqa: BLE001
        logger.warning("provider %s get_instruments 失败: %s", provider_name, e)
        return []
    logger.info("instruments via %s: %d %s", provider_name, len(rows), asset_type)
    return rows


def sync_instruments(data_dir: Path) -> int:
    """全量同步标的维表 → data/instruments/instruments.parquet。

    同时维护 ``instrument_status/history.parquet`` 的 SCD2 历史。历史从首次
    成功同步开始积累，不把当前名称或 ST 状态倒填到更早日期。

    返回写入的行数。
    """
    from app.services import preferences

    source = preferences.get_daily_data_provider()
    all_rows = _fetch_instruments_via_provider()
    if all_rows is None:
        # 未命中非 tickflow provider → 走 tickflow 直连
        source = "tickflow"
        tf = get_client()
        all_rows = []
        for ex in _EXCHANGES:
            try:
                items = tf.exchanges.get_instruments(ex, instrument_type="stock")
                if not items and ex in {"SH", "SZ"}:
                    logger.warning("empty %s catalog; keeping previous instruments", ex)
                    return 0
                if items:
                    all_rows.extend(_flatten_instruments(items))
                    logger.info("instruments %s: %d stocks", ex, len(items))
            except Exception as e:
                logger.warning("get_instruments(%s) failed: %s", ex, e)
                return 0

    if not all_rows:
        return 0

    # Enforce the same universe contract for every provider. A successful
    # snapshot is a full replacement, so stale/delisted rows disappear from
    # the current pool while update_instrument_history records the transition.
    all_rows = _flatten_instruments(all_rows)
    all_rows = list({row["symbol"]: row for row in all_rows}.values())
    all_rows.sort(key=lambda row: row["symbol"])
    if not all_rows:
        return 0

    as_of = cn_today()
    out = data_dir / "instruments" / "instruments.parquet"
    # Read failures abort before either snapshot is changed.
    previous = pl.read_parquet(out) if out.exists() else pl.DataFrame()
    candidate_boards = {row["board"] for row in all_rows if row.get("board")}
    if not previous.is_empty():
        previous_boards = {
            current_a_share_board(row.get("symbol"))
            for row in previous.iter_rows(named=True)
        }
        previous_boards.discard(None)
        if (
            set(CURRENT_A_SHARE_BOARDS).issubset(previous_boards)
            and not set(CURRENT_A_SHARE_BOARDS).issubset(candidate_boards)
        ):
            logger.warning(
                "incomplete instrument catalog (%s); keeping complete previous snapshot",
                sorted(candidate_boards),
            )
            return 0
    all_rows = _preserve_metadata(all_rows, previous, as_of=as_of, source=source)
    df = pl.DataFrame(all_rows, schema_overrides={
        "listing_date": pl.String,
        **{field: pl.Float64 for field in _PERSISTENT_METADATA if field != "listing_date"},
        **{f"{field}_source": pl.String for field in _PERSISTENT_METADATA},
        **{f"{field}_as_of": pl.Date for field in _PERSISTENT_METADATA},
    }).with_columns(
        pl.lit(as_of).alias("as_of"),
        pl.lit(source).alias("snapshot_source"),
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    update_instrument_history(data_dir, df, as_of=as_of, source=source)
    atomic_write_parquet(df, out)

    logger.info("instruments synced: %d rows → %s", df.height, out)
    return df.height


def enrich_names_from_quotes(
    data_dir: Path,
    quotes_data: list[dict],
) -> int:
    """从 quotes 响应中提取 name，更新 instruments 维表（兜底补充）。

    盘后 quotes.get(universes) 返回的数据中包含 ext.name，
    用来补充 instruments 中可能缺失的 name。
    """
    if not quotes_data:
        return 0

    # 构建 symbol → name 映射
    name_map: dict[str, str] = {}
    for q in quotes_data:
        symbol = q.get("symbol", "")
        ext = q.get("ext") or {}
        name = ext.get("name") or q.get("name", "")
        if symbol and name:
            name_map[symbol] = name

    if not name_map:
        return 0

    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return 0

    df = pl.read_parquet(inst_path)

    # 只更新空 name 的行
    updates = pl.DataFrame({
        "symbol": list(name_map.keys()),
        "_new_name": list(name_map.values()),
    })
    df = df.join(updates, on="symbol", how="left")
    df = df.with_columns(
        pl.when(pl.col("name").is_null() | (pl.col("name") == ""))
        .then(pl.col("_new_name"))
        .otherwise(pl.col("name"))
        .alias("name"),
    ).drop("_new_name")

    from app.services import preferences

    update_instrument_history(
        data_dir,
        df,
        as_of=cn_today(),
        source=preferences.get_daily_data_provider(),
    )
    atomic_write_parquet(df, inst_path)
    logger.info("instruments name enriched from quotes: %d names", len(name_map))
    return len(name_map)
