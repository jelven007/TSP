"""Three task pools share native socket quotas and retain the saved reference."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

GROUPS = ("realtime", "minute", "history")
_MINUTE_CATEGORIES = {0, 1, 2, 3, 7, 8}


def assign_candidates(nodes, *, bindings=None):
    ordered = list(dict.fromkeys(nodes))
    bindings = bindings or {}
    occupied = set(bindings.values())
    result = {}
    for position, group in enumerate(GROUPS):
        candidates = [node for node in ordered[position::3] if node not in occupied]
        binding = bindings.get(group)
        if binding in ordered:
            candidates.insert(0, binding)
        result[group] = candidates
    return result


def _servers(value):
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _during_session():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    # Holiday callers do not consume the reserve unless the exchange is open.
    from app.market_time import published_trading_day

    published = published_trading_day(now.date())
    trading = published if published is not None else now.weekday() < 5
    return trading and (9, 15) <= (now.hour, now.minute) <= (15, 5)


class TaskRuntime:
    """Lazy pools; construction opens no sockets and writes no configuration."""
    supports_task_options = True

    def __init__(self, factory, *, connections, timeout, reference, ranked_nodes=None,
                 fallback_servers=None, seed_nodes=None, background_connections=None):
        self.factory = factory
        self.max_connections = connections
        self.timeout = timeout
        self.background_connections = background_connections
        self.reference = reference
        self.pools = {}
        self.closed = False
        self._lock = threading.RLock()
        self._nodes = list(ranked_nodes) if ranked_nodes is not None else None
        self._seeds = list(seed_nodes or [])
        self._fallback = fallback_servers
        self._overrides = {
            group: os.getenv(f"RUSTDX_{group.upper()}_SERVER", "").strip()
            for group in GROUPS
        }
        self._backups = {
            group: _servers(os.environ[f"RUSTDX_{group.upper()}_FALLBACK_SERVERS"])
            if f"RUSTDX_{group.upper()}_FALLBACK_SERVERS" in os.environ else None
            for group in GROUPS
        }
        self._discovery = None
        self._snapshot = None
        self._qualifications = {}
        self._validation = {"windows": 0, "cache_hits": 0, "conflicts": 0,
                            "reference_requests": 0, "reference_seconds": 0.0}
        self._bindings = {"minute": self._overrides["minute"] or reference}
        self._bindings.update({group: value for group, value in self._overrides.items() if value})

    def _ranked(self):
        if self._nodes is not None:
            return self._nodes
        seeds = list(dict.fromkeys([self.reference, *self._seeds]))
        if not seeds:
            raise RuntimeError("rustdx initial node pool is empty")
        discovery = self.factory(
            1, seeds[0], self.timeout, fallback_servers=seeds[1:], task_group="realtime",
        )
        self._discovery = discovery
        try:
            self._snapshot = json.loads(discovery.node_pool_json(False))
            nodes = self._snapshot["valid_nodes"]
            if not nodes:
                raise RuntimeError("rustdx daily node qualification found no valid endpoint")
            self._nodes = nodes
        finally:
            discovery.close()
            self._discovery = None
        return self._nodes

    def _get(self, group):
        with self._lock:
            if self.closed:
                raise RuntimeError("rustdx task runtime is closed")
            if group in self.pools:
                return self.pools[group]
            # Explicit endpoints do not depend on unrelated discovery success.
            explicit = self._overrides[group] or (self.reference if group == "minute" else "")
            backups = self._backups[group]
            if group == "minute" and backups is None:
                backups = self._fallback
            if not explicit or backups is None:
                assigned = assign_candidates(self._ranked(), bindings=self._bindings)
                candidates = assigned[group]
                if explicit:
                    candidates = [explicit, *(node for node in candidates if node != explicit)]
                if backups is not None:
                    candidates = [candidates[0], *backups] if candidates else []
            else:
                candidates = [explicit, *backups]
            if not candidates:
                raise RuntimeError(f"rustdx {group} group has no qualified candidates")
            if self.closed:
                raise RuntimeError("rustdx task runtime is closed")
            background = self._minute_limit() if group == "minute" else min(
                self.max_connections, self.background_connections or self.max_connections,
            )
            pool = self.factory(
                self.max_connections, candidates[0], self.timeout,
                background_connections=background, fallback_servers=candidates[1:],
                task_group=group,
            )
            self._bindings[group] = candidates[0]
            self.pools[group] = pool
            return pool

    def quotes_json(self, securities, *, priority=False):
        return self._get("realtime").quotes_json(securities, priority=priority)

    def _minute_limit(self):
        limit = min(self.max_connections, self.background_connections or self.max_connections)
        if _during_session():
            return min(limit, 6, max(1, self.max_connections - 2))
        return limit

    def bars_json(self, market, code, category, *args, **options):
        group = "minute" if category in _MINUTE_CATEGORIES else "history"
        pool = self._get(group)
        if group == "minute":
            pool.set_background_connections(self._minute_limit())
        return pool.bars_json(market, code, category, *args, **options)

    def reference_bars_json(self, market, code, category, *args, **options):
        # The native pool replaces one existing slot, even when minute tasks use
        # another explicit main. All reference pages pin that same connection.
        pool = self._get("minute")
        pool.set_background_connections(self._minute_limit())
        return pool.bars_json(market, code, category, *args, reference_server=self.reference, **options)

    def validate_bar_window(self, rows, *, scope, source, reference_read):
        from .client import RustdxSourceChangedError
        from .provider import _as_beijing_datetime, _bar_fingerprint

        if not source or not source.get("endpoint"):
            raise RustdxSourceChangedError("rustdx 完整窗口来源身份缺失")
        if source["endpoint"] == self.reference:
            return rows
        _, frequency, start, end = scope
        start, end = _as_beijing_datetime(start), _as_beijing_datetime(end)
        if frequency in {9, 5} and end is not None:
            end = end.replace(hour=23, minute=59, second=59, microsecond=999999)

        def content(records):
            fingerprints = sorted(_bar_fingerprint(row) for row in records)
            return [item for item in fingerprints if item[0] is not None
                    and (start is None or item[0] >= start)
                    and (end is None or item[0] <= end)]

        candidate = content(rows)
        digest = hashlib.sha256(json.dumps(candidate, default=str).encode()).hexdigest()
        key = (tuple(scope), json.dumps(source, sort_keys=True), self.reference, digest)
        now = time.monotonic()
        with self._lock:
            if self._qualifications.get(key, 0) > now:
                self._validation["cache_hits"] += 1
                return rows
        started = time.monotonic()
        requests = 0
        try:
            reference = reference_read()
            if isinstance(reference, tuple):
                reference, requests = reference
        except Exception as error:
            requests = getattr(error, "requests", 0)
            raise
        finally:
            with self._lock:
                self._validation["windows"] += 1
                self._validation["reference_seconds"] += time.monotonic() - started
                self._validation["reference_requests"] += requests
        if candidate != content(reference):
            with self._lock:
                self._validation["conflicts"] += 1
            return reference
        with self._lock:
            # Exact input and endpoint generation only; no security-wide certification.
            self._qualifications = {key: expiry for key, expiry in self._qualifications.items()
                                    if expiry > now}
            if len(self._qualifications) >= 1024:
                self._qualifications.pop(next(iter(self._qualifications)))
            self._qualifications[key] = time.monotonic() + 30
        return rows

    def stocks_json(self, *args):
        return self._get("history").stocks_json(*args)

    def xdxr_json(self, *args):
        return self._get("history").xdxr_json(*args)

    def finance_json(self, *args):
        return self._get("history").finance_json(*args)

    def company_info(self, *args):
        return self._get("history").company_info(*args)

    def report_file(self, *args):
        return self._get("history").report_file(*args)

    def stats_json(self):
        with self._lock:
            groups = {group: json.loads(pool.stats_json()) for group, pool in self.pools.items()}
            result = next(iter(groups.values()), {}).copy()
            result.update({
                "max_connections": self.max_connections,
                "business_limit": 24, "probe_limit": 3, "total_connection_limit": 27,
                "task_groups": groups, "bindings": dict(self._bindings),
                "daily_nodes": self._snapshot, "reference_server": self.reference,
                "closed": self.closed,
                "window_validation": dict(self._validation),
            })
            return json.dumps(result)

    def close(self):
        # Set cancellation before taking the creation lock; discovery can be blocked.
        self.closed = True
        discovery = self._discovery
        if discovery is not None:
            discovery.close()
        with self._lock:
            for pool in self.pools.values():
                pool.close()
            self._qualifications.clear()
