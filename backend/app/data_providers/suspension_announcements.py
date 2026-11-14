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
_STOCK_EVENT = re.compile(
    r"(?<!子)(?:本公司|公司)(?:的)?(?:(?:A股)?股票|证券(?=\(证券品种:A股股票[,，]))"  # noqa: RUF001
    r"(?:\([^)]{0,100}\))?(?:将于|将自|自|于)" + _DATE
    + r"(?:\([^)]{0,20}\))?(?:(?:上午)?开市(?:时)?起|起)(?:开始|继续)?(停牌|复牌)"
)
_OFFICIAL_HOSTS = {"www.sse.com.cn", "static.sse.com.cn", "www.szse.cn",
                   "disc.static.szse.cn", "static.cninfo.com.cn"}


def _local(stamp):
    return stamp.astimezone(CN_TZ).replace(tzinfo=None) if stamp.tzinfo else stamp


def _title(text):
    compact = re.sub(r"\s|<[^>]+>", "", unicodedata.normalize("NFKC", text or ""))
    return re.sub(r"^[^:]{1,12}:", "", compact).replace(":", "")


def parse_announcement(symbol, text, *, announced_at, cutoff, source_url):
    """Only unambiguous own-stock opening events known by the selection cutoff."""
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
    for match in _STOCK_EVENT.finditer(compact):
        prefix = re.split(r"[。;]", compact[:match.start()])[-1]
        if re.search(r"(?:预计|拟|计划|可能|若|如)$", prefix):
            continue
        try:
            effective = datetime(*map(int, match.group(1, 2, 3))).date()
        except ValueError:
            continue
        if effective > cutoff.date():
            continue
        suffix = compact[match.end():match.end() + 20]
        if re.match(r"(?:[，,])?(?:一小时|半天|[0-9一二三四五六七八九十]+分钟)", suffix):  # noqa: RUF001
            continue
        suspended = match.group(4) == "停牌"
        if effective in events and events[effective]["is_suspended"] != suspended:
            return []
        events[effective] = {"symbol": symbol, "effective_date": effective,
                             "announced_on": announced_at.date(), "announced_at": announced_at,
                             "is_suspended": suspended, "source_url": source_url}
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
            if not re.search("停牌|复牌", title):
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
                 "url": urljoin("https://www.szse.cn", row.get("attachPath", ""))}
                for row in rows if code in row.get("secCode", []) and re.search("停牌|复牌", row["title"])]

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

    def __call__(self, symbol, day, *, cutoff, **kwargs):
        code, exchange = symbol.split(".")
        begin, end = day - timedelta(days=60), day + timedelta(days=1)
        notices = self._sse(code, begin, end) if exchange == "SH" else self._szse(code, begin, end)
        diagnostics, events = [], []
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
                parsed = parse_announcement(symbol, text, announced_at=stamp, cutoff=cutoff, source_url=url)
                events.extend(parsed)
                if not parsed:
                    diagnostics.append(f'{notice["title"]}: 正文未确认自身全天停复牌生效日')
            except (ValueError, httpx.HTTPError) as exc:
                diagnostics.append(f'{notice["title"]}: {exc}')
        if len(notices) > 8:
            diagnostics.append("公告超过8份附件查证上限")
        return {"events": events, "diagnostics": diagnostics}
