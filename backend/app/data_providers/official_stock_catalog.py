"""沪深交易所公开在册 A 股目录, 输出统一证券字段(股本单位: 股)。"""
from __future__ import annotations

import io
from datetime import date

import httpx
import polars as pl

from app.data_providers.instrument_status import (
    CURRENT_A_SHARE_BOARDS,
    current_a_share_board,
    current_a_share_identity,
    is_delisted_name,
    normalize_instrument_name,
)

SOURCE = "official_exchanges"
_UA = "Mozilla/5.0"
_SSE_REFERER = "https://www.sse.com.cn/assortment/stock/list/share/"
_SZSE_REFERER = "https://www.szse.cn/market/product/stock/list/index.html"


class OfficialCatalogError(RuntimeError):
    """官方目录下载失败或完整性校验失败; 不得替换已有快照。"""


def _listing_date(value) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise OfficialCatalogError("官方目录上市日期无效") from exc


def _row(code, name, exchange, listing_date, **metadata) -> dict:
    identity = current_a_share_identity(code=code, exchange=exchange)
    name = normalize_instrument_name(name)
    if identity is None or not name or is_delisted_name(name):
        raise OfficialCatalogError(f"{exchange} 官方在册目录含无效证券")
    symbol, code, exchange = identity
    return {
        "symbol": symbol, "code": code, "exchange": exchange, "name": name,
        "board": current_a_share_board(symbol), "type": "stock",
        "listing_date": _listing_date(listing_date),
        "snapshot_source": "sse" if exchange == "SH" else "szse",
        **metadata,
    }


def _fetch_sse(client: httpx.Client, stock_type: str) -> list[dict]:
    response = client.get(
        "https://query.sse.com.cn/sseQuery/commonQuery.do",
        params={
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
            "STOCK_TYPE": stock_type, "COMPANY_STATUS": "2,4,5,7,8",
            "type": "inParams", "isPagination": "true",
            "pageHelp.pageSize": 10000, "pageHelp.pageNo": 1,
            "pageHelp.beginPage": 1, "pageHelp.endPage": 1, "pageHelp.cacheSize": 1,
            "STOCK_CODE": "", "CSRC_CODE": "", "REG_PROVINCE": "",
        },
        headers={"User-Agent": _UA, "Referer": _SSE_REFERER},
    )
    response.raise_for_status()
    payload = response.json()
    records = payload.get("result", [])
    total = int(payload.get("pageHelp", {}).get("total", 0))
    if not records or total != len(records):
        raise OfficialCatalogError("沪交易所目录为空或分页不完整")
    # 状态包含停牌、暂停上市及退市整理; 有正式退市日期的证券不能进入当前池。
    return [
        _row(
            item["A_STOCK_CODE"], item["SEC_NAME_CN"], "SH", item["LIST_DATE"],
            region=item.get("AREA_NAME_DESC") or "CN",
            industry=item.get("CSRC_CODE_DESC"),
        )
        for item in records
        if str(item.get("DELIST_DATE") or "-").strip() in {"", "-"}
        and not is_delisted_name(item.get("SEC_NAME_CN"))
    ]


def _download_szse(client: httpx.Client) -> pl.DataFrame:
    response = client.get(
        "https://www.szse.cn/api/report/ShowReport",
        params={"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"},
        headers={"User-Agent": _UA, "Referer": _SZSE_REFERER},
    )
    response.raise_for_status()
    return pl.read_excel(io.BytesIO(response.content), engine="calamine", infer_schema_length=None)


def normalize_szse_catalog(frame: pl.DataFrame) -> list[dict]:
    required = {"A股代码", "A股简称", "A股上市日期"}
    if frame.is_empty() or not required.issubset(frame.columns):
        raise OfficialCatalogError("深交易所目录为空或缺少必需字段")
    rows = []
    for item in frame.iter_rows(named=True):
        if is_delisted_name(item.get("A股简称")):
            continue
        metadata = {"region": item.get("省    份") or "CN", "industry": item.get("所属行业")}
        for column, field in (("A股总股本", "total_shares"), ("A股流通股本", "float_shares")):
            text = str(item.get(column) or "").replace(",", "").strip()
            metadata[field] = float(text) if text else None
        rows.append(_row(item["A股代码"], item["A股简称"], "SZ", item["A股上市日期"], **metadata))
    return rows


def fetch_official_stock_catalog(*, client: httpx.Client | None = None) -> list[dict]:
    """必须一次成功取得两市及四板块; 任何异常都拒绝整个候选快照。"""
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=True) as owned:
            return fetch_official_stock_catalog(client=owned)
    try:
        rows = _fetch_sse(client, "1") + _fetch_sse(client, "8")
        frame = _download_szse(client)
        # JSON 元数据独立核对下载的 XLSX 行数, 防止部分文件被误认为全量目录。
        response = client.get(
            "https://www.szse.cn/api/report/ShowReport/data",
            params={"SHOWTYPE": "JSON", "CATALOGID": "1110", "TABKEY": "tab1", "PAGENO": 1},
            headers={"User-Agent": _UA, "Referer": _SZSE_REFERER},
        )
        response.raise_for_status()
        total = int(response.json()[0]["metadata"]["recordcount"])
        if total != frame.height:
            raise OfficialCatalogError("深交易所下载目录与官方总数不符")
        rows.extend(normalize_szse_catalog(frame))
        symbols = [row["symbol"] for row in rows]
        if len(set(symbols)) != len(symbols):
            raise OfficialCatalogError("官方目录含重复证券")
        if {r["board"] for r in rows} != set(CURRENT_A_SHARE_BOARDS):
            raise OfficialCatalogError("官方目录缺少沪深 A 股板块")
        return sorted(rows, key=lambda row: row["symbol"])
    except OfficialCatalogError:
        raise
    except Exception as exc:
        raise OfficialCatalogError("沪深官方目录下载或解析失败") from exc
