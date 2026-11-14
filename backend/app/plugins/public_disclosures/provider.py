"""Source adapter; callers own persistence and coverage, never infer empty success."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx


def number(text):
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


class PublicDisclosuresProvider:
    name = "public_disclosures"
    builtin = True

    def __init__(self, client=None):
        self.config = SimpleNamespace(
            name=self.name, datasets={"money_flow": {}, "sina_money_flow": {}, "announcements": {}},
            builtin=True, path=None,
        )
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(25, connect=8), follow_redirects=False,
            headers={"User-Agent": "TSP public data synchronization"},
        )

    def close(self):
        self.client.close()

    def _json(self, url, params, *, allow_list=False):
        # Transport retries are bounded; business errors are not disguised as [].
        for attempt in range(3):
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) and not (allow_list and isinstance(payload, list)):
                    raise ValueError("数据源响应不是对象")
                return payload
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def get_money_flow(self, symbol: str, start: date, end: date) -> list[dict]:
        code, exchange = symbol.split(".")
        if exchange not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
            raise ValueError("非法股票标识")
        payload = self._json(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            {"secid": f"{1 if exchange == 'SH' else 0}.{code}", "lmt": 10000,
             "klt": 101, "fields1": "f1,f2,f3,f7",
             "fields2": ",".join(f"f{i}" for i in range(51, 64))},
        )
        data = payload.get("data")
        if payload.get("rc") != 0 or not isinstance(data, dict) or data.get("code") != code:
            raise ValueError(f"{symbol} 资金流业务响应无效")
        lines = data.get("klines")
        if not isinstance(lines, list):
            raise ValueError(f"{symbol} 资金流历史缺失")
        observed = datetime.now(UTC).isoformat()
        rows = []
        amount_names = ["main", "small", "medium", "large", "super_large"]
        for line in lines:
            parts = line.split(",")
            if len(parts) != 13:
                raise ValueError(f"{symbol} 资金流字段不完整")
            day = date.fromisoformat(parts[0])
            if not start <= day <= end:
                continue
            row = {
                "symbol": symbol, "date": str(day), "source": "eastmoney",
                "observed_at": observed, "publication_time_verified": False,
                "close": number(parts[11]),
                "change_ratio": (v / 100 if (v := number(parts[12])) is not None else None),
            }
            for i, name in enumerate(amount_names, 1):
                row[f"{name}_net_inflow"] = number(parts[i])
                ratio = number(parts[i + 5])
                row[f"{name}_net_ratio"] = None if ratio is None else ratio / 100
            rows.append(row)
        return rows

    def get_sina_money_flow(
        self, symbol: str, start: date, end: date, *, page_size: int = 200,
    ) -> list[dict]:
        """Sina's four native order classes; never splice into Eastmoney fields."""
        code, exchange = symbol.split(".")
        if exchange not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
            raise ValueError("非法股票标识")
        observed = datetime.now(UTC).isoformat()
        rows, seen = [], set()
        previous = None
        for page in range(1, 201):
            items = self._json(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "MoneyFlow.ssl_qsfx_lscjfb",
                {"daima": exchange.lower() + code, "page": page, "num": page_size,
                 "sort": "opendate", "asc": 0},
                allow_list=True,
            )
            if not isinstance(items, list):
                raise ValueError(f"{symbol} 新浪资金流业务响应无效")
            for item in items:
                day = date.fromisoformat(item["opendate"])
                if day in seen or (previous is not None and day >= previous):
                    raise ValueError("新浪资金流分页日期重复或顺序改变")
                seen.add(day)
                previous = day
                if not start <= day <= end:
                    continue
                row = {
                    "symbol": symbol, "date": str(day), "source": "sina",
                    "observed_at": observed, "publication_time_verified": False,
                    "net_inflow": number(item.get("netamount")),
                    "net_ratio": number(item.get("ratioamount")),
                    "close": number(item.get("trade")),
                    "change_ratio": number(item.get("changeratio")),
                }
                for i, name in enumerate(("super_large", "large", "small", "retail")):
                    row[f"{name}_net_inflow"] = number(item.get(f"r{i}_net"))
                rows.append(row)
            if not items or previous <= start or len(items) < page_size:
                return sorted(rows, key=lambda row: row["date"])
        raise ValueError("新浪资金流分页达到安全上限")

    def get_announcements(
        self, day: date, code_lookup: dict[str, str], *, page_size: int = 100,
    ) -> list[dict]:
        rows, seen = [], set()
        expected = None
        observed = datetime.now(UTC).isoformat()
        # Annual reporting peaks exceed 20,000 notices in one day.
        for page in range(1, 1001):
            payload = self._json(
                "https://np-anotice-stock.eastmoney.com/api/security/ann",
                {"page_size": page_size, "page_index": page, "ann_type": "A",
                 "begin_time": str(day), "end_time": str(day)},
            )
            data = payload.get("data")
            if payload.get("success") != 1 or not isinstance(data, dict):
                raise ValueError("公告业务响应无效")
            total = data.get("total_hits")
            items = data.get("list")
            if not isinstance(total, int) or total < 0 or not isinstance(items, list):
                raise ValueError("公告分页缺少总数")
            if expected is None:
                expected = total
            elif total != expected:
                raise ValueError("公告分页过程中总数改变, 需重试")
            for item in items:
                aid = item.get("art_code")
                if not aid or aid in seen:
                    raise ValueError("公告分页重复或缺少标识")
                if str(item.get("notice_date", ""))[:10] != str(day):
                    raise ValueError("公告日期超出请求范围")
                seen.add(aid)
                for security in item.get("codes") or []:
                    symbol = code_lookup.get(security.get("stock_code"))
                    if symbol:
                        rows.append({
                            "symbol": symbol, "date": str(day), "announcement_id": aid,
                            "title": item.get("title"), "display_time": item.get("display_time"),
                            "source": "eastmoney", "observed_at": observed,
                            "publication_time_verified": False,
                            "categories": "|".join(
                                x.get("column_name", "") for x in item.get("columns") or []
                            ),
                            "source_url": f"https://data.eastmoney.com/notices/detail/{symbol[:6]}/{aid}.html",
                        })
            if len(seen) == expected:
                return rows
            if len(items) < page_size or len(seen) > expected:
                raise ValueError("公告分页未获取到声明的全部记录")
        raise ValueError("公告分页达到安全上限")
