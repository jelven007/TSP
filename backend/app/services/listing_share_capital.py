"""Dated listing-capital evidence supplements, never overwrites, financial reports."""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet
from app.share_capital import apply_historical_float_shares, load_share_history


def parse_listing_capital(text: str, symbol: str, listing_day: date) -> dict:
    compact = re.sub(r"\s+", "", text).replace("\uff0c", ",")
    code = symbol[:6]
    if not re.search(rf"(?:股票|证券)代码[:\uff1a]?{code}", compact):
        raise ValueError("上市公告股票代码不匹配")
    if f"{listing_day.year}年{listing_day.month}月{listing_day.day}日" not in compact:
        raise ValueError("上市公告缺少已知上市日期")
    number = r"[为:\uff1a]*([0-9][0-9,]*(?:\.[0-9]+)?)(万股|亿股|股)"
    labels = {
        "total_shares": r"本次(?:公开)?发行(?:完成)?后的?(?:总股本|股份总数)",
        "float_shares": r"本次(?:上市的|公开发行后)无流通限制及(?:限售|锁定)安排的(?:股票|股份)数量",
    }
    result = {}
    for field, label in labels.items():
        matches = list(re.finditer(label + number, compact))
        values = {
            Decimal(match[1].replace(",", "")) * {"股": 1, "万股": 10000, "亿股": 100000000}[match[2]]
            for match in matches
        }
        if len(values) != 1:
            raise ValueError(f"上市公告{field}缺失或相互矛盾")
        value = values.pop()
        if value <= 0 or value != value.to_integral_value():
            raise ValueError("上市股本不是正整数股")
        result[field] = float(value)
    if result["float_shares"] > result["total_shares"]:
        raise ValueError("上市流通股本大于总股本")
    return result


def missing_listing_instruments(data_dir, instruments, expected):
    if "listing_date" not in instruments.columns:
        return []
    rows = [
        {"symbol": symbol, "date": min(days), "float_shares": None}
        for symbol, days in expected.items() if days
    ]
    if not rows:
        return []
    keys = pl.from_dicts(rows, schema={
        "symbol": pl.String, "date": pl.Date, "float_shares": pl.Float64,
    })
    history = load_share_history(Path(data_dir))
    floating = apply_historical_float_shares(keys, history, today=date.max)
    total_history = (
        history.drop("float_shares").rename({"total_shares": "float_shares"})
        if "total_shares" in history.columns else pl.DataFrame()
    )
    total = apply_historical_float_shares(keys, total_history, today=date.max)
    missing = set(floating.filter(pl.col("float_shares").is_null())["symbol"])
    missing.update(total.filter(pl.col("float_shares").is_null())["symbol"])
    return [
        row for row in instruments.to_dicts()
        if row["symbol"] in missing and row.get("listing_date")
    ]


def persist_listing_events(data_dir, records):
    """Caller holds the related-data process lock; distinct evidence is immutable."""
    if not records:
        return
    directory = Path(data_dir) / "financials/share_capital_events"
    evidence = directory / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    for record in records:
        content, digest = record["pdf_bytes"], record["evidence_sha256"]
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("上市公告摘要不匹配")
        target = evidence / f"{digest}.pdf"
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("已有上市公告摘要不匹配")
            continue
        with tempfile.NamedTemporaryFile(dir=evidence, suffix=".tmp", delete=False) as stream:
            stream.write(content)
            temp = Path(stream.name)
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    path = directory / "part.parquet"
    frame = pl.from_dicts([{k: v for k, v in row.items() if k != "pdf_bytes"} for row in records])
    if path.exists():
        frame = pl.concat([pl.read_parquet(path), frame], how="diagonal_relaxed")
    atomic_write_parquet(
        frame.unique(["symbol", "effective_date", "source"], keep="last"), path,
    )


def fetch_listing_capital(provider, instrument):
    """Verify issuer PDF, its security, dates, explicit units and both share values."""
    from pypdf import PdfReader

    symbol = instrument["symbol"]
    listing_day = date.fromisoformat(str(instrument["listing_date"])[:10])
    payload = provider._json(
        "https://np-anotice-stock.eastmoney.com/api/security/ann",
        {"page_size": 100, "page_index": 1, "ann_type": "A", "stock_list": symbol[:6],
         "begin_time": str(listing_day-timedelta(days=10)), "end_time": str(listing_day)},
    )
    data = payload.get("data") or {}
    if payload.get("success") != 1 or not isinstance(data.get("total_hits"), int):
        raise ValueError("上市公告业务响应无效")
    if data["total_hits"] > 100:
        raise ValueError("上市公告搜索超过单页, 不能声明完整")
    notices = [
        item for item in data.get("list", [])
        if "上市公告书" in item.get("title", "") and "提示" not in item["title"]
        and not item["title"].endswith("更正公告")
        and any(security.get("stock_code") == symbol[:6] for security in item.get("codes", []))
    ]
    if not notices:
        raise ValueError("没有股票对应的完整上市公告书")
    notice = sorted(notices, key=lambda item: (item["notice_date"], item["art_code"]))[-1]
    if not re.fullmatch(r"AN\d+", notice["art_code"]):
        raise ValueError("上市公告标识无效")
    # The observed issuer attachment format works independently of the text API.
    url = f"https://pdf.dfcfw.com/pdf/H2_{notice['art_code']}_1.pdf"
    content = bytearray()
    with provider.client.stream("GET", url) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 100 * 1024 * 1024:
                raise ValueError("上市公告PDF超过大小上限")
    if not content.startswith(b"%PDF"):
        raise ValueError("上市公告附件不是PDF")
    reader = PdfReader(io.BytesIO(content))
    if len(reader.pages) > 1000:
        raise ValueError("上市公告PDF超过页数上限")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    values = parse_listing_capital(text, symbol, listing_day)
    return {
        "symbol": symbol, "announce_date": notice["notice_date"][:10],
        "effective_date": str(listing_day), **values, "source": "issuer_listing_document",
        "source_url": url, "announcement_id": notice["art_code"],
        "evidence_sha256": hashlib.sha256(content).hexdigest(),
        "observed_at": datetime.now(UTC).isoformat(), "publication_time_verified": False,
        "pdf_bytes": bytes(content),
    }
