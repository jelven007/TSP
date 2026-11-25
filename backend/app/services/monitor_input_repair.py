"""Bounded, cancellable requests to the platform's existing collection services."""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from pathlib import Path

from app.market_time import as_cn_datetime, cn_now
from app.services.fs_utils import atomic_write_text


def merge_proof(previous, incoming):
    """Retain two distinct successful requests per symbol, dependency and source day."""
    from copy import deepcopy
    proof = deepcopy(previous)
    for symbol, dependencies in incoming.items():
        for dependency, receipts in dependencies.items():
            combined = proof.setdefault(symbol, {}).setdefault(dependency, []) + receipts
            days = {}
            for receipt in combined:
                days.setdefault(receipt.get("day"), {})[receipt["request_seq"]] = receipt
            proof[symbol][dependency] = [
                receipts[seq] for _, receipts in sorted(days.items(), key=lambda item: str(item[0]))
                for seq in sorted(receipts)[-2:]]
    return proof


class MonitorInputRepair:
    def __init__(self, state, *, now=cn_now):
        self.state, self.now = state, now
        self.root = Path(state.repo.store.data_dir) / "monitor_input_repairs"
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="monitor-input-repair")
        self.active = set()
        self.stopped = False

    def close(self):
        self.stopped = True
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _read(self, key):
        try:
            return json.loads((self.root / f"{key}.json").read_text())
        except FileNotFoundError:
            return {}

    def _save(self, key, value):
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.root / f"{key}.json", json.dumps(value, ensure_ascii=False))
        return value

    @staticmethod
    def _owner(owner, sid):
        return "generation-" + hashlib.sha256(f"{owner}:{sid}".encode()).hexdigest()

    def cancel(self, owner, sid, generation):
        with self.lock:
            key = self._owner(owner, sid)
            self._save(key, {"generation": max(generation, self._read(key).get("generation", 0))})
            return {"state": "cancelled", "generation": generation}

    def check(self, request):
        now = as_cn_datetime(self.now())
        deadline = as_cn_datetime(datetime.fromisoformat(request["retry_until"]))
        if (self.stopped or self._read(self._owner(request["owner"], request["strategy_id"])).get(
                "generation", 0) > request["generation"]):
            raise ValueError("监控已取消, 输入修复已结束")
        if now > deadline or (request["phase"] == "breadth" and now >= deadline):
            raise ValueError("任务修复期限已结束")

    def request(self, request):
        with self.lock:
            self.check(request)
            for active in sorted(self.active):
                saved = self._read(active)
                prior = saved["request"]
                if (any(prior.get(field) != request.get(field) for field in (
                        "owner", "strategy_id", "generation", "day", "phase",
                        "retry_until", "certify_coverage"))
                        or not set(request["symbols"]) <= set(prior["symbols"])
                        or not set(request["daily_dates"]) <= set(prior["daily_dates"])):
                    continue
                if request.get("minute_end") and (
                        not prior.get("minute_end")
                        or prior["minute_end"] < request["minute_end"]
                        or (prior.get("minute_start") or "09:30:00") >
                           (request.get("minute_start") or "09:30:00")):
                    continue
                return saved
            identity = {key: value for key, value in request.items() if key != "attempt"}
            identity["symbols"] = sorted(set(request["symbols"]))
            identity["daily_dates"] = sorted(set(request["daily_dates"]))
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            previous = self._read(key)
            successful = previous.get("successes", 0)
            if key in self.active or (previous.get("state") == "succeeded" and (
                    not request.get("certify_coverage") or successful >= 2)):
                return previous
            value = {"id": key, "state": "queued", "request": request,
                     "attempts": previous.get("attempts", 0) + 1,
                     "successes": successful, "result": previous.get("result", {}),
                     "started_at": self.now().isoformat(), "retry_until": request["retry_until"]}
            self._save(key, value)
            self.active.add(key)
            self.pool.submit(self._run, key, value)
            return value

    def _run(self, key, value):
        from app.services import pipeline_jobs
        owner = "monitor-input-repair:" + key
        acquired = False
        try:
            self.check(value["request"])
            acquired = pipeline_jobs.try_acquire_run_slot(owner)
            if not acquired:
                raise ValueError("行情写入服务忙, 主任务在有效期限内重试")
            self._save(key, {**value, "state": "running"})
            result = self.repair(value["request"])
            self.check(value["request"])
            result["proof"] = merge_proof(
                value.get("result", {}).get("proof", {}), result.get("proof", {}))
            value["successes"] += 1
            value.update(state="succeeded", result=result)
        except Exception as exc:
            value.update(state="failed", error=str(exc))
        finally:
            if acquired:
                pipeline_jobs.release_run_slot(owner)
            with self.lock:
                self._save(key, {**value, "completed_at": self.now().isoformat()})
                self.active.discard(key)

    def repair(self, request):
        """Provider IO and writes remain exclusively in this platform service."""
        import polars as pl

        from app.indicators.pipeline import run_pipeline
        from app.services import kline_periods
        from app.services.daily_close_validation import require_final_daily_rows
        from app.services.minute_write_order import begin_minute_request
        from app.services.realtime_guard import paused_quotes

        repo = self.state.repo
        capset = getattr(self.state, "capabilities", None)
        quotes = getattr(self.state, "quote_service", None)
        symbols = request["symbols"]
        day = date.fromisoformat(request["day"])
        written, proof = {}, {}
        periods = [("1d", date.fromisoformat(value), time.max) for value in request["daily_dates"]]
        if request["phase"] == "breadth":
            missing = []
            for period, source, end in periods:
                self.check(request)
                try:
                    raw, _ = repo.get_daily_close_inputs(source, include_closing_minute=False)
                    raw = raw.filter(pl.col("symbol").is_in(symbols))
                    require_final_daily_rows(raw, source)
                    if set(raw["symbol"].to_list()) != set(symbols):
                        missing.append((period, source, end))
                except (ValueError, OSError, AttributeError, pl.exceptions.PolarsError):
                    missing.append((period, source, end))
            periods = missing
        if request.get("minute_end"):
            minute = ("1m", day, time.fromisoformat(request["minute_end"]))
            if request["phase"] == "breadth":
                periods.insert(0, minute)
            else:
                periods.append(minute)
        for period, source, end in periods:
            self.check(request)
            provider, _ = kline_periods.require_period_provider(period, capset)
            count = 0
            for offset in range(0, len(symbols), 100):
                self.check(request)
                sequence = begin_minute_request(repo.store.data_dir / "kline_minute") if period == "1m" else None
                batch = symbols[offset:offset + 100]
                start = (time.fromisoformat(request["minute_start"]) if period == "1m"
                         and request.get("minute_start") else time(9, 30) if period == "1m" else time.min)
                frame = kline_periods.fetch_period(
                    provider, period, batch, datetime.combine(source, start),
                    datetime.combine(source, end), minute_adjust="none")
                # A blocking provider returning after cancellation/deadline cannot write.
                with self.lock, paused_quotes(quotes):
                    self.check(request)
                    if not frame.is_empty():
                        kwargs = {"request_seq": sequence} if sequence is not None else {}
                        kline_periods.persist_period(repo, period, frame, **kwargs)
                        count += frame.height
                    if request.get("certify_coverage"):
                        for symbol in batch:
                            proof.setdefault(symbol, {}).setdefault(
                                "minute" if period == "1m" else "daily", []).append({
                                    "symbol": symbol, "day": str(source), "state": "succeeded",
                                    "request_seq": sequence or time_module_ns(),
                                    "required_from": datetime.combine(source, start).isoformat(),
                                    "completed_through": datetime.combine(source, end).isoformat(),
                                    "collected_at": self.now().isoformat(),
                                    "returned_rows": (frame.filter(pl.col("symbol") == symbol).height
                                                      if "symbol" in frame.columns else 0),
                                })
            if not count and not request.get("certify_coverage"):
                raise ValueError(f"{source} {period}修复未返回所需股票数据")
            self.check(request)
            kline_periods.refresh_period(repo, period)
            if period == "1d" and request["phase"] != "breadth":
                with paused_quotes(quotes):
                    self.check(request)
                    run_pipeline(data_dir=repo.store.data_dir, symbols=symbols,
                                 on_batch_done=lambda *_: self.check(request))
                    self.check(request)
                repo.refresh_cache()
            if period == "1m" and len(periods) > 1 and request["phase"] == "breadth":
                publisher = getattr(self.state, "public_publisher", None)
                if publisher:
                    self.check(request)
                    publisher.publish()
            written[f"{period}:{source}"] = count
        self.check(request)
        if request["phase"] == "select":
            from app.services.post_close_sync import finalize_daily_settlement
            release = finalize_daily_settlement(repo, capset, day, lambda: self.check(request))
        else:
            publisher = getattr(self.state, "public_publisher", None)
            release = publisher.publish() if publisher else None
        return {"written_rows": written, "release": release, "proof": proof}


def time_module_ns():
    import time
    return time.time_ns()
