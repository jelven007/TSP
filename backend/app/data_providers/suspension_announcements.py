"""Bounded official notice lookup; missing prices are never halt evidence."""
from __future__ import annotations

import io
import re
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from pypdf import PdfReader

from app.market_time import CN_TZ

_DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日"
_OPEN = r"(?:(?:上午)?开市(?:时)?(?:起)?|起)"
_JOINT_BONDS = (r"(?:(?:[、,，]|及|和|以及)(?:可转换公司(?:债券|转债)|可转债)"  # noqa: RUF001
                r"(?:债券|转股)?(?:\([^)]{0,100}\))?){0,3}")
_STOCK_EVENT = re.compile(
    r"(?<!子)(?:本公司|公司)(?:\((?:以下简称|下称)[^)]{1,40}\))?(?:的)?"
    r"(?:(?:A股)?股票|证券(?=\((?:证券品种:)?A股股票[:,，])"  # noqa: RUF001
    r"|证券(?=\([^)]{0,100}(?:证券|股票)代码:))"
    r"(?:交易)?(?:\([^)]{0,100}\))?" + _JOINT_BONDS + r"(?:已)?(?:将于|将自|自|于)" + _DATE
    + r"(?:\([^)]{0,40}\))?(?:" + _OPEN
    + r"|(?=停牌(?:1|一)(?:天|日|个交易日)))(?:开始|继续|连续)?(停牌|复牌)"
)
_FOLLOWING_RESUME = re.compile(
    r"^(?:1|一)(?:天|日|个交易日)[,，](?:并)?(?:将于|将自|自|于)?"  # noqa: RUF001
    + _DATE + r"(?:\([^)]{0,20}\))?" + _OPEN + r"复牌"
)
_TABLE_DATE = r"(\d{4})/(\d{1,2})/(\d{1,2})(?=\D|$|\d{4}/|\d{6}[^\d/])"
_STOCK_TABLE = re.compile(
    r"(?<!\d)(\d{6})[^\d。;]{1,30}A股停牌" + _TABLE_DATE
    + r"全天" + _TABLE_DATE + _TABLE_DATE
)
_OPEN_TABLE = re.compile(
    r"(?<!\d)(\d{6})[^\d。;]{1,30}A股(停牌)" + _TABLE_DATE
    + r"(?!(?:上午|下午|半天|[0-9一二三四五六七八九十]+分钟))"
)
_RESUME_TABLE = re.compile(
    r"(?<!\d)(\d{6})[^\d。;]{1,30}A股复牌" + _TABLE_DATE + _TABLE_DATE
)
_DELISTING_RESUME = re.compile(
    r"(?<!子)(?:本公司|公司)股票(?:将于|于)" + _DATE
    + r"(?:\([^)]{0,20}\))?进入退市整理期交易"
)
_DELISTING_START = re.compile(
    r"(?<!子)(?:本公司|公司)股票进入退市整理期(?:交易)?的起始日为" + _DATE
)
_OFFICIAL_HOSTS = {"www.sse.com.cn", "static.sse.com.cn", "www.szse.cn",
                   "disc.static.szse.cn", "static.cninfo.com.cn"}
_NOTICE_EVENT_TITLE = re.compile(r"停牌|复牌|进入退市整理期|终止上市.*摘牌")


def _status_event_title(title):
    from app.services.issuer_st_status import _event_notice
    return bool(_NOTICE_EVENT_TITLE.search(title) or _event_notice(title))


def _local(stamp):
    return stamp.astimezone(CN_TZ).replace(tzinfo=None) if stamp.tzinfo else stamp


def _title(text):
    compact = re.sub(r"\s|<[^>]+>", "", unicodedata.normalize("NFKC", text or ""))
    return re.sub(r"^[^:]{1,12}:", "", compact).replace(":", "")


def parse_announcement(symbol, text, *, announced_at, cutoff, source_url, collect_future=False):
    """Collect future effective events only for the independent discovery path."""
    if announced_at is None:
        return []
    announced_at, cutoff = _local(announced_at), _local(cutoff)
    if announced_at > cutoff or urlparse(source_url).scheme != "https" or (
            urlparse(source_url).hostname not in _OFFICIAL_HOSTS):
        return []
    code = symbol.split(".")[0]
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if not re.search(r"(?:证券|股票)代码:?" + re.escape(code) + r"(?!\d)", compact):
        return []
    events = {}
    def add(effective, suspended):
        if not collect_future and effective > cutoff.date():
            return
        if effective in events and events[effective]["is_suspended"] != suspended:
            raise ValueError("conflicting body")
        events[effective] = {"symbol": symbol, "effective_date": effective,
                             "announced_on": announced_at.date(), "announced_at": announced_at,
                             "is_suspended": suspended, "source_url": source_url}

    # A typed own-A-share row is explicit full-day evidence, unlike a bond row.
    for table in _OPEN_TABLE.finditer(compact):
        if table.group(1) != code:
            continue
        try:
            effective = datetime(*map(int, table.group(3, 4, 5))).date()
            add(effective, table.group(2) == "停牌")
        except ValueError:
            return []
    for table in _STOCK_TABLE.finditer(compact):
        if table.group(1) != code:
            continue
        try:
            start = datetime(*map(int, table.group(2, 3, 4))).date()
            finish = datetime(*map(int, table.group(5, 6, 7))).date()
            resume = datetime(*map(int, table.group(8, 9, 10))).date()
            if start <= finish < resume:
                add(start, True)
                add(resume, False)
        except ValueError:
            return []
    for table in _RESUME_TABLE.finditer(compact):
        if table.group(1) != code:
            continue
        try:
            finish = datetime(*map(int, table.group(2, 3, 4))).date()
            resume = datetime(*map(int, table.group(5, 6, 7))).date()
            if finish < resume:
                add(resume, False)
        except ValueError:
            return []
    # Trading in a confirmed delisting period ends the preceding full-day halt.
    for pattern in (_DELISTING_RESUME, _DELISTING_START):
        for match in pattern.finditer(compact):
            try:
                add(datetime(*map(int, match.group(1, 2, 3))).date(), False)
            except ValueError:
                return []
    for match in _STOCK_EVENT.finditer(compact):
        if re.match(r"(?:本公司|公司)(?:的)?证券", match.group()):
            identity = re.search(r"证券\(([^)]*)\)", match.group())
            if identity and not re.match(r"(?:证券品种:)?A股股票[:,，]", identity[1]) and (  # noqa: RUF001
                    not re.search(r"(?:证券|股票)代码:[^)]*" + re.escape(code) + r"(?!\d)", identity[1])
                    or re.search(r"债券|转债", identity[1])):
                continue
        prefix = re.split(r"[。;]", compact[:match.start()])[-1]
        if re.search(r"(?:预计|拟|计划|可能|若|如)$", prefix):
            continue
        try:
            effective = datetime(*map(int, match.group(1, 2, 3))).date()
        except ValueError:
            continue
        suffix = compact[match.end():match.end() + 20]
        if re.match(r"(?:[，,])?(?:一小时|半天|[0-9一二三四五六七八九十]+分钟)", suffix):  # noqa: RUF001
            continue
        suspended = match.group(4) == "停牌"
        try:
            add(effective, suspended)
            resume = _FOLLOWING_RESUME.match(compact[match.end():])
            if suspended and resume:
                resume_day = datetime(*map(int, resume.group(1, 2, 3))).date()
                if resume_day > effective:
                    add(resume_day, False)
        except ValueError:
            return []
    return [events[day] for day in sorted(events)]


class AnnouncementLookup:
    """Reuse one HTTP pool, with hard request/elapsed/PDF/page bounds per attempt."""
    def __init__(self, *, check_cancelled, max_requests=96, seconds=60, client=None):
        self.check_cancelled = check_cancelled
        self.remaining = max_requests
        self.deadline = time.monotonic() + seconds
        self.client = client or httpx.Client(
            timeout=8, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/"},
        )
        self.owned = client is None

    def close(self):
        if self.owned:
            self.client.close()

    def _request(self, method, url, **kwargs):
        self.check_cancelled()
        remaining = self.deadline - time.monotonic()
        if self.remaining <= 0 or remaining <= 0:
            raise ValueError("停复牌查证请求/时间预算已耗尽, 下轮重试")
        self.remaining -= 1
        with self.client.stream(method, url, timeout=min(8, remaining), **kwargs) as response:
            response.raise_for_status()
            parts, total = [], 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                self.check_cancelled()
                total += len(chunk)
                if total > 4 * 1024**2 or time.monotonic() > self.deadline:
                    raise ValueError("公告响应超过4MB或查证时间预算")
                parts.append(chunk)
            headers = {key: value for key, value in response.headers.items()
                       if key not in {"content-encoding", "content-length"}}
            return httpx.Response(response.status_code, content=b"".join(parts),
                                  headers=headers, request=response.request)

    def _pdf(self, url):
        if urlparse(url).hostname not in _OFFICIAL_HOSTS or urlparse(url).scheme != "https":
            raise ValueError("公告附件不是官方HTTPS来源")
        response = self._request("GET", url)
        if len(response.content) > 4 * 1024**2 or not response.content.startswith(b"%PDF"):
            raise ValueError("官方附件不是可读取PDF或超过4MB")
        if urlparse(str(response.url)).hostname not in _OFFICIAL_HOSTS:
            raise ValueError("公告附件跳转到非官方来源")
        reader = PdfReader(io.BytesIO(response.content), strict=True)
        if reader.is_encrypted or len(reader.pages) > 30:
            raise ValueError("公告PDF加密或超过30页")
        parts = []
        for page in reader.pages:
            self.check_cancelled()
            if time.monotonic() > self.deadline:
                raise ValueError("公告解析时间预算已耗尽")
            parts.append(page.extract_text() or "")
        return "\n".join(parts)

    def _sse(self, code, begin, end):
        response = self._request("GET", "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do",
                                 params={"isPagination": "true", "securityType": "0101,120100,020100",
                                         "productId": code, "beginDate": str(begin), "endDate": str(end),
                                         "bulletinType": "L2_ALL", "pageHelp.pageSize": 100,
                                         "pageHelp.pageNo": 1})
        body = response.json()
        rows = body.get("result")
        if not isinstance(rows, list):
            raise ValueError("上交所公告列表格式无效")
        notices = []
        for row in rows:
            if str(row.get("SECURITY_CODE", row.get("PRODUCTID", ""))) != code:
                continue
            title = row.get("TITLE", "")
            if not _NOTICE_EVENT_TITLE.search(title):
                continue
            stamp = row.get("ADDDATE", "")
            try:
                stamp = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                stamp = None
            if stamp and stamp.time() == datetime.min.time():
                stamp = None
            notices.append({"title": title, "formal_date": str(row.get("SSEDATE", ""))[:10],
                            "announced_at": stamp,
                            "url": urljoin("https://www.sse.com.cn", row.get("URL", ""))})
        if len(rows) >= 100:
            raise ValueError("上交所公告列表超过单次查证上限")
        return notices

    def _szse(self, code, begin, end):
        body = self._request("POST", "https://www.szse.cn/api/disc/announcement/annList",
                             headers={"Referer": "https://www.szse.cn/"},
                             json={"seDate": [str(begin), str(end)], "stock": [code],
                                   "channelCode": ["listedNotice_disc"], "pageSize": 100,
                                   "pageNum": 1}).json()
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ValueError("深交所公告列表格式无效")
        if body.get("announceCount", 0) > 100:
            raise ValueError("深交所公告列表超过单次查证上限")
        return [{"title": row["title"], "formal_date": str(row.get("publishTime", ""))[:10],
                 # publishTime is an announcement date, not actual public availability.
                 "announced_at": None,
                 "url": urljoin("https://disc.static.szse.cn/download/",
                                row.get("attachPath", "").lstrip("/"))}
                for row in rows if code in row.get("secCode", []) and _NOTICE_EVENT_TITLE.search(row["title"])]

    def _public_times(self, code, begin, end):
        body = self._request("GET", "https://np-anotice-stock.eastmoney.com/api/security/ann",
                             headers={"Referer": "https://data.eastmoney.com/"},
                             params={"page_size": 100, "page_index": 1, "ann_type": "A",
                                     "stock_list": code, "begin_time": str(begin),
                                     "end_time": str(end)}).json()
        rows = (body.get("data") or {}).get("list")
        if not isinstance(rows, list):
            raise ValueError("公告公开时间索引格式无效")
        times = {}
        for row in rows:
            if not any(str(item.get("stock_code")) == code for item in row.get("codes", [])):
                continue
            try:
                stamp = datetime.fromisoformat(str(row["display_time"])[:19])
            except (KeyError, ValueError):
                continue
            title = row.get("title", "")
            for item in row.get("codes", []):
                prefix = str(item.get("short_name", "")) + ":"
                if str(item.get("stock_code")) == code and title.startswith(prefix):
                    title = title[len(prefix):]
                    break
            key = (_title(title), str(row.get("notice_date", ""))[:10])
            # Duplicate public titles with different clocks are ambiguous.
            times[key] = stamp if key not in times or times[key] == stamp else None
        return times

    def _cninfo_alternative(self, code, notice, begin, end):
        """Resolve official security identity before matching an alternate copy."""
        identities = self._request(
            "POST", "https://www.cninfo.com.cn/new/information/topSearch/query",
            headers={"Referer": "https://www.cninfo.com.cn/"},
            data={"keyWord": code, "maxSecNum": 10, "maxListNum": 5}).json()
        org_ids = {row["orgId"] for row in identities
                   if row.get("code") == code and row.get("orgId")}
        if len(org_ids) != 1:
            raise ValueError("未找到唯一巨潮证券身份")
        column = "sse" if code.startswith("6") else "szse"
        body = self._request("POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                             headers={"Referer": "https://www.cninfo.com.cn/"},
                             data={"pageNum": 1, "pageSize": 100, "column": column,
                                   "tabName": "fulltext", "stock": f"{code},{next(iter(org_ids))}",
                                   "seDate": f"{begin}~{end}", "sortName": "time",
                                   "sortType": "desc", "isHLtitle": "true"}).json()
        if body.get("hasMore") or body.get("totalAnnouncement", 0) > 100:
            raise ValueError("巨潮公告列表超过单次查证上限")
        matches = []
        for row in body.get("announcements") or []:
            if row.get("secCode") != code or _title(row.get("announcementTitle")) != _title(notice["title"]):
                continue
            stamp = datetime.fromtimestamp(row["announcementTime"] / 1000, CN_TZ).date()
            if str(stamp) != notice["formal_date"]:
                continue
            matches.append(urljoin("https://static.cninfo.com.cn/", row.get("adjunctUrl", "")))
        if len(set(matches)) != 1:
            raise ValueError("未找到同证券/标题/公告日期的唯一巨潮官方附件")
        return matches[0]

    def market_notices(self, exchange, begin, end, *, checkpoint=None, on_progress=None):
        """Complete all-market lists, never interpreting a truncated page as coverage."""
        from app.data_providers.instrument_status import current_a_share_identity

        if exchange not in {"SH", "SZ"}:
            raise ValueError("不支持的停复牌公告市场")
        page_size = 100 if exchange == "SH" else 50
        def page_keys(body):
            rows = body.get("result" if exchange == "SH" else "data") or []
            return [((str(row.get("SECURITY_CODE", "")),),
                     urljoin("https://www.sse.com.cn", row.get("URL", "")))
                    if exchange == "SH" else
                    (tuple(row.get("secCode", [])),
                     urljoin("https://disc.static.szse.cn/download/",
                             row.get("attachPath", "").lstrip("/")))
                    for row in rows]

        def total_rows(body):
            return (body.get("pageHelp") or {}).get("total") if exchange == "SH" else body.get("announceCount")

        def invalid(message):
            if on_progress:
                on_progress(None)
            raise ValueError(message)

        notices, seen, total, first_keys, page = [], set(), None, None, 0
        if checkpoint:
            if checkpoint["begin"] != str(begin) or checkpoint["end"] != str(end):
                raise ValueError("停复牌公告分页断点窗口不符")
            first = self._market_page(exchange, begin, end, 1)
            first_keys = [(tuple(codes), url) for codes, url in checkpoint["first_keys"]]
            if total_rows(first) != checkpoint["total"] or page_keys(first) != first_keys:
                invalid("全市场公告扫描期间发生变化, 下轮重新扫描")
            notices, total, page = list(checkpoint["notices"]), checkpoint["total"], checkpoint["page"]
            seen = {(tuple(codes), url) for codes, url in checkpoint["seen"]}
        pages = range(page + 1, 65) if total is None or page * page_size < total else ()
        for page in pages:
            body = self._market_page(exchange, begin, end, page)
            rows = body.get("result" if exchange == "SH" else "data")
            count = total_rows(body)
            if not isinstance(rows, list) or type(count) is not int or not 0 <= count <= 64 * page_size:
                invalid("全市场公告分页格式或总量无效")
            if total is None:
                total = count
            if count != total or len(rows) != min(page_size, max(0, total - (page - 1) * page_size)):
                invalid("全市场公告分页总量变化或缺页, 下轮重新扫描")
            keys = []
            for row in rows:
                if exchange == "SH":
                    codes = [str(row.get("SECURITY_CODE", ""))]
                    title, url = row.get("TITLE"), row.get("URL")
                    formal_date = str(row.get("SSEDATE", ""))[:10]
                    stamp = row.get("ADDDATE")
                    try:
                        stamp = datetime.fromisoformat(stamp)
                        if stamp.time() == datetime.min.time():
                            stamp = None
                    except (TypeError, ValueError):
                        stamp = None
                    url = urljoin("https://www.sse.com.cn", url or "")
                else:
                    codes = row.get("secCode")
                    title, url = row.get("title"), row.get("attachPath")
                    formal_date = str(row.get("publishTime", ""))[:10]
                    stamp = None
                    url = urljoin("https://disc.static.szse.cn/download/", (url or "").lstrip("/"))
                if not isinstance(codes, list) or not isinstance(title, str) or not url.endswith((".PDF", ".pdf")):
                    invalid("全市场公告记录缺少证券/标题/附件身份")
                key = (tuple(codes), url)
                if key in seen:
                    invalid("全市场公告重复页或重复记录, 下轮重新扫描")
                seen.add(key)
                keys.append(key)
                if not _status_event_title(title):
                    continue
                datetime.fromisoformat(formal_date)
                for code in codes:
                    symbol = f"{code}.{exchange}"
                    if current_a_share_identity(symbol) is not None:
                        notices.append({"symbol": symbol, "title": title, "formal_date": formal_date,
                                        "announced_at": stamp.isoformat() if stamp else None, "url": url})
            if page == 1:
                first_keys = keys
            if on_progress:
                on_progress({"begin": str(begin), "end": str(end), "page": page,
                             "total": total, "first_keys": first_keys, "seen": sorted(seen),
                             "notices": list(notices)})
            if page * page_size >= total:
                break
        if page > 1:
            check = self._market_page(exchange, begin, end, 1)
            if total_rows(check) != total or page_keys(check) != first_keys:
                invalid("全市场公告扫描期间发生变化, 下轮重新扫描")
        return {"notices": notices, "pages": page, "rows": total}

    def _market_page(self, exchange, begin, end, page):
        if exchange == "SH":
            return self._request(
                "GET", "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do",
                params={"isPagination": "true", "securityType": "0101,120100,020100",
                        "beginDate": str(begin), "endDate": str(end), "bulletinType": "L2_ALL",
                        "pageHelp.pageSize": 100, "pageHelp.pageNo": page,
                        "pageHelp.beginPage": page, "pageHelp.endPage": page,
                        "pageHelp.cacheSize": 1}).json()
        return self._request(
            "POST", "https://www.szse.cn/api/disc/announcement/annList",
            headers={"Referer": "https://www.szse.cn/"},
            json={"seDate": [str(begin), str(end)], "stock": [],
                  "channelCode": ["listedNotice_disc"], "pageSize": 50, "pageNum": page}).json()

    def verify_notice(self, notice, *, cutoff):
        notice = dict(notice)
        if isinstance(notice.get("announced_at"), str):
            notice["announced_at"] = datetime.fromisoformat(notice["announced_at"])
        formal = datetime.fromisoformat(notice["formal_date"]).date()
        return self._resolve_notices(notice["symbol"], [notice], formal - timedelta(days=1),
                                     formal + timedelta(days=1), cutoff=cutoff, collect_future=True)

    def __call__(self, symbol, day, *, cutoff, **kwargs):
        code, exchange = symbol.split(".")
        begin, end = day - timedelta(days=60), day + timedelta(days=1)
        notices = self._sse(code, begin, end) if exchange == "SH" else self._szse(code, begin, end)
        return self._resolve_notices(symbol, notices, begin, end, cutoff=cutoff)

    def _resolve_notices(self, symbol, notices, begin, end, *, cutoff, collect_future=False):
        code = symbol.split(".")[0]
        diagnostics, events, deferred = [], [], False
        risk_events = []
        retry_category = "semantic"
        times = None
        for notice in sorted(notices, key=lambda item: item["formal_date"], reverse=True)[:8]:
            self.check_cancelled()
            if notice["announced_at"] is None:
                if times is None:
                    dated = [datetime.fromisoformat(item["formal_date"]).date()
                             for item in notices if item["formal_date"]]
                    times = self._public_times(code, min(dated) - timedelta(days=1), max(dated))
                notice["announced_at"] = times.get((_title(notice["title"]), notice["formal_date"]))
            stamp = notice["announced_at"]
            if collect_future and stamp is not None and _local(stamp) > _local(cutoff):
                deferred = True
                continue
            if stamp is None or _local(stamp) > _local(cutoff):
                reason = ("未证明实际公开时间" if stamp is None else
                          f"公开时间{stamp.isoformat()}晚于预选截止{cutoff.isoformat()}")
                diagnostics.append(f'{notice["title"]}: {reason}')
                continue
            url = notice["url"]
            try:
                try:
                    text = self._pdf(url)
                except (ValueError, httpx.HTTPError):
                    url = self._cninfo_alternative(code, notice, begin, end)
                    text = self._pdf(url)
                parsed = parse_announcement(symbol, text, announced_at=stamp, cutoff=cutoff,
                                            source_url=url, collect_future=collect_future)
                events.extend(parsed)
                from app.services.issuer_st_status import _event_notice, parse_issuer_st_events
                risk_parsed = []
                if collect_future and _event_notice(notice["title"]):
                    risk_parsed = parse_issuer_st_events(
                        notice["title"], unicodedata.normalize("NFKC", text), symbol, _local(stamp).date())
                    risk_events.extend({
                        "symbol": symbol, "effective_date": event["effective_date"],
                        "announced_on": _local(stamp).date(), "announced_at": _local(stamp),
                        "is_risk_warning": event["is_risk_warning_after"], "source_url": url,
                        "evidence_text": text,
                    } for event in risk_parsed)
                # Conversion-only notices have been read, but have no stock event.
                conversion_only = (
                    "转股停牌" in _title(notice["title"])
                    and "股票" not in _title(notice["title"])
                    and re.search(r"(?<!\d)\d{6}[^\d。;]{1,30}可转债转股停牌", re.sub(r"\s+", "", text))
                    and not re.search(r"(?:A股|股票)(?:交易)?[^。;]{0,80}(?:停牌|复牌)", re.sub(r"\s+", "", text))
                )
                if not parsed and not risk_parsed and not conversion_only:
                    diagnostics.append(f'{notice["title"]}: 正文未确认自身全天停复牌生效日')
            except (ValueError, httpx.HTTPError) as exc:
                diagnostics.append(f'{notice["title"]}: {exc}')
                retry_category = "transport"
        if len(notices) > 8:
            diagnostics.append("公告超过8份附件查证上限")
        return {"events": events, "risk_warning_events": risk_events,
                "diagnostics": diagnostics, "deferred": deferred,
                "retry_category": retry_category}
