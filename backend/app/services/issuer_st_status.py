"""Dated issuer PDF evidence for missing or contradictory F10 ST history."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.st_history_backfill import classify_treatment


def _event_notice(title):
    return (
        "风险警示" in title and any(word in title for word in ("实施", "撤销"))
        and not any(word in title for word in ("申请", "可能", "提示", "进展", "预计"))
    )


def parse_issuer_st_events(title, text, symbol, announced):
    compact = re.sub(r"\s+", "", text)
    if not _event_notice(title) or not re.search(rf"(?:股票|证券)代码[:\uff1a]?{symbol[:6]}", compact):
        raise ValueError("ST 公告不是已实施事件或股票代码不匹配")
    state, classification = classify_treatment(title)
    events = []
    for match in re.finditer(
        r"股票(?:交易)?自(\d{4})年(\d{1,2})月(\d{1,2})日起([^。\uff0c,;\uff1b]{1,90})",
        compact,
    ):
        clause = match[4]
        if "风险警示" not in clause:
            continue
        clause_state, _ = classify_treatment(clause)
        # A removal document may describe its old application date as context.
        if clause_state != state:
            continue
        events.append({
            "symbol": symbol, "announcement_date": announced,
            "effective_date": date(*(int(match[i]) for i in (1, 2, 3))),
            "treatment_type": title, "other": clause,
            "is_risk_warning_after": state, "classification": classification,
        })
    if not events:
        raise ValueError("ST 公告没有明确日期的股票状态证据")
    return events


def fetch_issuer_st_evidence(provider, instrument, start, end, *, _history_start=None):
    from pypdf import PdfReader

    symbol = instrument["symbol"]
    notices, seen, total = [], set(), None
    for page in range(1, 101):
        payload = provider._json(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            {"page_size": 100, "page_index": page, "ann_type": "A", "stock_list": symbol[:6],
             "begin_time": str(_history_start or start - timedelta(days=730)), "end_time": str(end)},
        )
        data = payload.get("data") or {}
        count = data.get("total_hits")
        batch = data.get("list")
        if (
            payload.get("success") != 1 or not isinstance(count, int) or count < 0
            or count > 10000 or not isinstance(batch, list)
            or (total is not None and total != count)
        ):
            raise ValueError("ST 公告分页业务响应无效")
        total = count
        for notice in batch:
            identity = notice.get("art_code")
            if not identity or identity in seen:
                raise ValueError("ST 公告分页重复或缺少标识")
            seen.add(identity)
            if _event_notice(notice.get("title", "")) and any(
                security.get("stock_code") == symbol[:6] for security in notice.get("codes", [])
            ):
                notices.append(notice)
        if len(seen) >= total:
            break
        if len(batch) < 100:
            raise ValueError("ST 公告分页提前结束")
    if len(seen) != total:
        raise ValueError("ST 公告分页未完整")
    documents, events = [], []
    for notice in sorted(notices, key=lambda row: (row["notice_date"], row["art_code"]), reverse=True):
        if not re.fullmatch(r"AN\d+", notice["art_code"]):
            raise ValueError("ST 公告标识无效")
        url = f"https://pdf.dfcfw.com/pdf/H2_{notice['art_code']}_1.pdf"
        content = bytearray()
        with provider.client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 25 * 1024 * 1024:
                    raise ValueError("ST 公告PDF超过大小上限")
        if not content.startswith(b"%PDF"):
            raise ValueError("ST 公告附件不是PDF")
        reader = PdfReader(io.BytesIO(content))
        if len(reader.pages) > 200:
            raise ValueError("ST 公告PDF超过页数上限")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        announced = date.fromisoformat(notice["notice_date"][:10])
        parsed = parse_issuer_st_events(notice["title"], text, symbol, announced)
        events.extend(parsed)
        documents.append({
            "announcement_id": notice["art_code"], "title": notice["title"],
            "announce_date": str(announced), "source_url": url, "text": text,
            "evidence_sha256": hashlib.sha256(content).hexdigest(), "pdf_bytes": bytes(content),
        })
        # Newer changes plus a disclosure predating the requested start suffice.
        # Older, unrelated warning episodes need not be reinterpreted.
        if announced <= start and any(row["effective_date"] <= start for row in parsed):
            break
    if not events or min(row["effective_date"] for row in events) > start:
        listing = instrument.get("listing_date")
        listing = date.fromisoformat(str(listing)[:10]) if listing else None
        if _history_start is None and listing and listing < start - timedelta(days=730):
            return fetch_issuer_st_evidence(
                provider, instrument, start, end, _history_start=listing,
            )
        raise ValueError("ST 公告未覆盖历史区间起点")
    ordered = sorted(events, key=lambda row: (row["effective_date"], row["announcement_date"]))
    latest = [row for row in ordered if row["effective_date"] <= end][-1]
    if latest["is_risk_warning_after"] != ("ST" in (instrument.get("name") or "").upper()):
        raise ValueError("ST 公告最新状态与当前名称矛盾")
    digest = hashlib.sha256("".join(sorted(row["evidence_sha256"] for row in documents)).encode()).hexdigest()
    return {
        "source": "issuer_disclosure", "events": ordered, "documents": documents,
        "evidence_sha256": digest, "observed_at": datetime.now(UTC).isoformat(),
    }


def persist_issuer_st_evidence(data_dir, symbol, evidence):
    directory = Path(data_dir) / "instrument_status/issuer_announcements" / symbol
    directory.mkdir(parents=True, exist_ok=True)
    documents = []
    for document in evidence["documents"]:
        content, digest = document["pdf_bytes"], document["evidence_sha256"]
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("ST 公告摘要不匹配")
        target = directory / f"{digest}.pdf"
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("已有 ST 公告摘要不匹配")
        else:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                stream.write(content)
                temp = Path(stream.name)
            try:
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        documents.append({k: v for k, v in document.items() if k != "pdf_bytes"})
    atomic_write_text(
        directory / f"{evidence['evidence_sha256']}.json",
        json.dumps({**evidence, "documents": documents}, ensure_ascii=False, default=str),
    )
