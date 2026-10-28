"""Failure-aware Python facade for the native rustdx connection pool."""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from ._native_identity import CORE_SHA256, PATCH_SHA256

_RUSTDX_CRATE_VERSION = "2.0.4"
_RUSTDX_SOURCE_REV = "c4c0273456874898aed50447fd900521d9ffd3b8"
_MAX_CONNECTIONS = 8
_DEFAULT_SERVER = "117.34.114.13:7709"
_NATIVE_LOCK = threading.Lock()
_NATIVE_CLIENT = None
_NATIVE_CONFIG = None


class RustdxError(RuntimeError):
    """rustdx protocol, bridge, or connection failure."""

    def __init__(self, message: str, *, requests: int = 0) -> None:
        super().__init__(message)
        self.requests = requests


class RustdxSourceChangedError(RustdxError):
    """A paginated result must restart after its source changes."""


def _connection_count() -> int:
    try:
        configured = int(os.getenv("RUSTDX_QUOTE_CONNECTIONS", str(_MAX_CONNECTIONS)))
    except ValueError:
        configured = _MAX_CONNECTIONS
    return max(1, min(configured, _MAX_CONNECTIONS))


def _timeout_seconds() -> int:
    try:
        configured = int(os.getenv("RUSTDX_TIMEOUT", "8"))
    except ValueError:
        configured = 8
    return max(2, min(configured, 60))


def _background_connections() -> int:
    try:
        value = int(os.getenv("RUSTDX_BACKGROUND_CONNECTIONS", "7"))
    except ValueError:
        value = 7
    return max(1, min(value, max(1, _connection_count() - 1)))


def _native_client():
    global _NATIVE_CLIENT, _NATIVE_CONFIG
    with _NATIVE_LOCK:
        config = tuple(os.getenv(key, "") for key in (
            "RUSTDX_SERVER", "RUSTDX_FALLBACK_SERVERS", "RUSTDX_TIMEOUT",
            "RUSTDX_QUOTE_CONNECTIONS", "RUSTDX_BACKGROUND_CONNECTIONS",
            "RUSTDX_FINANCIAL_SERVER", "RUSTDX_NETWORK_SHARED_DIR",
        ))
        if _NATIVE_CLIENT is not None and config != _NATIVE_CONFIG:
            _NATIVE_CLIENT.close()
            _NATIVE_CLIENT = None
            _NATIVE_CONFIG = None
        if _NATIVE_CLIENT is None:
            module = importlib.import_module("tsp_rustdx_native")
            available, detail = availability()
            if not available:
                raise RustdxError(detail)
            server = os.getenv("RUSTDX_SERVER", _DEFAULT_SERVER).strip() or _DEFAULT_SERVER
            _NATIVE_CLIENT = module.RustdxClient(
                _connection_count(),
                server,
                _timeout_seconds(),
                background_connections=_background_connections(),
                fallback_servers=[
                    value.strip()
                    for value in os.getenv(
                        "RUSTDX_FALLBACK_SERVERS",
                        "" if os.getenv("RUSTDX_SERVER") else "115.238.56.198:7709,180.153.18.170:7709",
                    ).split(",")
                    if value.strip()
                ],
            )
            _NATIVE_CONFIG = config
        return _NATIVE_CLIENT


def availability() -> tuple[bool, str]:
    """Plugin check used by the loader without opening a network connection."""
    try:
        module = importlib.import_module("tsp_rustdx_native")
        maximum = int(module.MAX_CONNECTIONS)
        version = str(module.__version__)
        rustdx_version = str(module.RUSTDX_VERSION)
        rustdx_source_rev = str(module.RUSTDX_SOURCE_REV)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        return False, f"缺少 rustdx 原生桥接: {exc}"
    if maximum != _MAX_CONNECTIONS or getattr(module, "PROBE_CONNECTIONS", 0) != 1:
        return False, f"rustdx 原生桥接连接上限异常: {maximum}"
    if (
        rustdx_version != _RUSTDX_CRATE_VERSION
        or rustdx_source_rev != _RUSTDX_SOURCE_REV
    ):
        return False, "rustdx 原生桥接依赖版本与项目锁定版本不一致"
    if getattr(module, "POOL_API_VERSION", 0) != 3:
        return False, "rustdx 原生桥接需要重新构建: 连接池接口版本不一致"
    if (
        getattr(module, "RUSTDX_CORE_SHA256", "") != CORE_SHA256
        or getattr(module, "RUSTDX_PATCH_SHA256", "") != PATCH_SHA256
    ):
        return False, "rustdx 原生桥接源码或补丁身份不一致, 需要重新构建"
    return (
        True,
        f"jelven-rustdx-complete {_RUSTDX_CRATE_VERSION} "
        f"(jelven007/rustdx@{_RUSTDX_SOURCE_REV[:7]}) / bridge {version} "
        f"/ patch {PATCH_SHA256[:12]} / 8业务+1探测",
    )


def close_native_pool() -> None:
    global _NATIVE_CLIENT, _NATIVE_CONFIG
    with _NATIVE_LOCK:
        if _NATIVE_CLIENT is not None:
            _NATIVE_CLIENT.close()
            _NATIVE_CLIENT = None
        _NATIVE_CONFIG = None


def _security(symbol: str) -> tuple[int, str]:
    from app.market_scope import removed_market_symbol
    if removed_market_symbol(symbol):
        raise RustdxError(f"无效交易所: {symbol}")
    code, separator, exchange = str(symbol or "").upper().partition(".")
    if len(code) != 6 or not code.isdigit():
        raise RustdxError(f"无效证券代码: {symbol}")
    if not separator:
        exchange = "SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else ""
    if exchange not in {"SH", "SZ"}:
        raise RustdxError(f"无效交易所: {symbol}")
    return (1 if exchange == "SH" else 0), code


def _is_index(market: int, code: str) -> bool:
    return (market == 1 and code.startswith(("000", "880", "999"))) or (
        market == 0 and code.startswith("399")
    )


def _records(payload: str) -> list[dict[str, Any]]:
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise RustdxError(f"rustdx 返回无效 JSON: {exc}") from exc
    return _record_rows(rows)


def _record_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise RustdxError("rustdx 返回结构不是数组")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _datetime_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        year = int(value["year"])
        month = int(value["month"])
        day = int(value["day"])
        hour = int(value.get("hour", 15))
        minute = int(value.get("minute", 0))
    except (KeyError, TypeError, ValueError):
        return None
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"


class RustdxClient:
    """One facade over eight business connections and one separate probe."""

    def __enter__(self) -> RustdxClient:
        _native_client()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def close(self) -> None:
        """Individual facades do not own the shared native pool."""

    @staticmethod
    def close_shared() -> None:
        close_native_pool()

    @property
    def max_connections(self) -> int:
        return int(_native_client().max_connections)

    def stats(self) -> dict[str, Any]:
        return dict(json.loads(_native_client().stats_json()))

    def quotes(self, codes: list[str]) -> list[dict]:
        if not codes:
            return []
        try:
            securities = [_security(code) for code in codes]
            rows = _records(_native_client().quotes_json(securities))
        except RustdxError:
            raise
        except Exception as exc:
            raise RustdxError(f"rustdx quotes 调用失败: {exc}") from exc
        for row in rows:
            for side in ("bid", "ask"):
                for level in range(1, 6):
                    field = f"{side}{level}_vol"
                    if field in row:
                        row[f"{side}_vol{level}"] = row.pop(field)
            for field in ("vol", "amount"):
                if row.get(field) == 2.0**-127:
                    row[field] = 0.0
        return rows

    def bars(
        self,
        code: str,
        *,
        frequency: int,
        start: int = 0,
        offset: int = 800,
    ) -> list[dict]:
        rows, _, _ = self._bars(code, frequency=frequency, start=start, offset=offset, counted=False)
        return rows

    def bars_with_count(self, code: str, *, frequency: int, offset: int) -> tuple[list[dict], int]:
        rows, requests, _ = self._bars(code, frequency=frequency, start=0, offset=offset, counted=True)
        return rows, requests

    def bars_with_source(
        self, code: str, *, frequency: int, start: int, offset: int,
        expected_source: dict | None = None,
    ) -> tuple[list[dict], dict]:
        rows, _, source = self._bars(
            code, frequency=frequency, start=start, offset=offset,
            counted=True, sourced=True, expected_source=expected_source,
        )
        if not source:
            raise RustdxSourceChangedError("rustdx K线来源身份缺失")
        return rows, source

    def _bars(
        self, code: str, *, frequency: int, start: int, offset: int, counted: bool,
        sourced: bool = False, expected_source: dict | None = None,
    ):
        market, raw_code = _security(code)
        index = _is_index(market, raw_code)
        wire_frequency = 4 if index and frequency == 9 else frequency
        requests = 0
        source = None
        try:
            payload = _native_client().bars_json(
                market,
                raw_code,
                wire_frequency,
                start,
                min(max(offset, 1), 800),
                index,
                **({"with_request_count": True} if counted else {}),
                **({"with_source": True, "expected_source": json.dumps(expected_source)
                    if expected_source else None} if sourced else {}),
            )
            if counted:
                result = json.loads(payload)
                requests = int(result["requests"])
                if result.get("error") is not None:
                    error_type = RustdxSourceChangedError if "[SourceChanged]" in result["error"] else RustdxError
                    raise error_type(
                        f"rustdx minute {code} failed after {requests} requests: {result['error']}",
                        requests=requests,
                    )
                rows = _record_rows(result["rows"])
                source = result.get("source")
            else:
                rows, requests = _records(payload), 1
        except RustdxError:
            raise
        except Exception as exc:
            raise RustdxError(f"rustdx bars 调用失败: {exc}", requests=requests) from exc
        for row in rows:
            row["datetime"] = _datetime_text(row.pop("dt", None))
            for field in ("vol", "amount"):
                if row.get(field) == 2.0**-127:
                    row[field] = 0.0
            if index and frequency in {0, 1, 2, 3, 7, 8}:
                row["vol"] = None
        return rows, requests, source

    def stocks(self, market: int) -> list[dict]:
        if market not in {0, 1}:
            raise RustdxError(f"rustdx 不支持市场代码: {market}")
        try:
            return _records(_native_client().stocks_json(market))
        except Exception as exc:
            raise RustdxError(f"rustdx stocks 调用失败: {exc}") from exc

    def xdxr(self, code: str) -> list[dict]:
        market, raw_code = _security(code)
        try:
            return _records(_native_client().xdxr_json(market, raw_code))
        except Exception as exc:
            raise RustdxError(f"rustdx xdxr 调用失败: {exc}") from exc

    def finance(self, code: str) -> list[dict]:
        market, raw_code = _security(code)
        try:
            return _records(_native_client().finance_json(market, raw_code))
        except Exception as exc:
            raise RustdxError(f"rustdx finance 调用失败: {exc}") from exc

    def company_info(self, code: str, category_name: str) -> str | None:
        market, raw_code = _security(code)
        try:
            return _native_client().company_info(market, raw_code, category_name)
        except Exception as exc:
            raise RustdxError(f"rustdx F10 调用失败: {exc}") from exc

    @staticmethod
    def financial_history(
        symbols: list[str],
        *,
        periods: int,
        columns: set[str],
        cache_dir: Path,
    ) -> list[dict]:
        from app.plugins.rustdx.financial import financial_history

        class ReportTransport:
            def report_file(self, filename, max_bytes):
                server = os.getenv("RUSTDX_FINANCIAL_SERVER", "120.76.152.87:7709").strip()
                return _native_client().report_file(
                    filename,
                    max_bytes,
                    server or "120.76.152.87:7709",
                )

        try:
            return financial_history(
                ReportTransport(),
                symbols,
                periods=periods,
                columns=columns,
                cache_dir=cache_dir,
            )
        except Exception as exc:
            raise RustdxError(f"rustdx 历史财务读取失败: {exc}") from exc


def main() -> int:
    """Validate the installed bridge before launching or packaging the app."""
    available, detail = availability()
    if not available:
        print(
            f"{detail}\n"
            "源码环境安装 Rust 编译工具后, 在 backend 目录执行 "
            "uv sync --frozen --reinstall-package tsp-rustdx-native; "
            "Docker 或桌面包请重新构建包含原生桥接的产物。",
            file=sys.stderr,
        )
        return 1
    print(f"rustdx 原生桥接已就绪: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
