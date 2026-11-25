"""S02权威输入发布服务: 后台补依赖, 首次ready排他封存, 镜像端只接收。

文件封存不表示策略成功; Worker/Compute仍校验绑定、计算和提交。
当前契约见docs/reference/s02.md。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.strategy_input_manifest import (
    canonical,
    common_identity,
    common_key,
    digest,
    minute_bounds,
    validate_manifest,
)

KEY = re.compile(r"^[0-9a-f]{64}$")


class InputStore:
    """按共同定义键和阶段保存不可变输入, 读取时重新校验语义。"""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, key, phase):
        """限制键为SHA-256和两个阶段, 避免路径越界或混用业务文件。"""
        if not isinstance(key, str) or not KEY.fullmatch(key) or phase not in ("stocks", "breadth"):
            raise ValueError("输入阶段身份无效")
        return self.root / key / f"{phase}.json"

    def read_key(self, key, phase):
        """缺文件返回None; 损坏或身份不匹配抛错, 不能当作无数据。"""
        path = self.path(key, phase)
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        validate_manifest(value)
        if common_key(value["input"]) != key or value["phase"] != phase:
            raise ValueError("输入存储身份不一致")
        return value

    def read(self, request, phase):
        """由请求共同身份定位阶段文件。"""
        return self.read_key(common_key(request), phase)

    def baseline(self, key):
        """验证首次共享基线摘要, 供另一阶段复用同一池及预选。"""
        path = self.path(key, "stocks").with_name("baseline.json")
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        if value["sha256"] != digest(value["input"]) or common_key(value["input"]) != key:
            raise ValueError("输入基线身份无效")
        return value["input"]

    def source_proof(self, key):
        """读取并验证已封存日线采集证明, 分钟证明仍由各阶段保存。"""
        path = self.path(key, "stocks").with_name("baseline.json")
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        self.baseline(key)
        proof = value.get("source_proof", {})
        if value.get("source_proof_sha256") != digest(proof):
            raise ValueError("基线采集证据摘要不一致")
        return proof

    def seal(self, value):
        """只封存ready; 首次基线/阶段文件胜出, 晚修订须一致且不能覆盖。"""
        validate_manifest(value)
        if value["state"] != "ready":
            raise ValueError("输入阶段尚未就绪, 不能封存")
        key, phase = common_key(value["input"]), value["phase"]
        path = self.path(key, phase)
        path.parent.mkdir(parents=True, exist_ok=True)
        baseline = {**common_identity(value["input"]),
                    "pool": deepcopy(value["input"]["pool"]),
                    "selection": deepcopy(value["input"]["selection"]),
                    "source_evidence": {key: deepcopy(item) for key, item in
                        value["input"]["source_evidence"].items() if key not in {
                            "reference_daily", "reference_daily_sha256",
                            "reference_errors", "daily_reference_errors"}}}
        base_path = path.with_name("baseline.json")
        proof = {symbol: {"daily": saved["proof"]["daily"]} for symbol, saved in value["coverage"].items()
                 if saved.get("proof", {}).get("daily")}
        with suppress(FileExistsError):
            atomic_write_text(base_path, canonical({
                "input": baseline, "sha256": digest(baseline),
                "source_proof": proof, "source_proof_sha256": digest(proof)}).decode(), overwrite=False)
        if digest(self.baseline(key)) != digest(baseline):
            raise ValueError("阶段输入与首次封存基线不一致")
        # 排他创建后读回胜出的文件; 重复调用不覆盖, 晚修订不能重开已封存阶段。
        with suppress(FileExistsError):
            atomic_write_text(path, canonical(value).decode(), overwrite=False)
        return self.read_key(key, phase)


class S02InputService:
    """Platform后台补数及封存, 不让行情等待占Compute槽位。

    ready重读仍在请求线程校验; 线程池只承担未就绪输入的后台推进。
    """

    def __init__(self, state, *, mirror=None):
        self.state, self.mirror = state, mirror
        self.store = InputStore(Path(state.repo.store.data_dir) / "s02_inputs")
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="s02-input")
        self.lock = threading.RLock()
        self.active, self.next_at, self.status, self.source_cache = set(), {}, {}, {}
        self.stopped = False

    def close(self):
        """停止新推进和未启动任务, 并关闭镜像客户端; 在途任务仍检查停止状态。"""
        self.stopped = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.mirror:
            self.mirror.close()

    @staticmethod
    def expired(request, phase):
        """个股允许截止整秒, 广度要求严格早于截止; 使用北京时间。"""
        from app.market_time import as_cn_datetime, cn_now
        if not request.get("retry_until"):
            return False
        deadline = as_cn_datetime(datetime.fromisoformat(request["retry_until"]))
        now = cn_now()
        return now > deadline or (phase == "breadth" and now >= deadline)

    def request(self, request, phase):
        """校验当日目标标签; 已封存清单现场复验, 未封存才后台推进。"""
        from app.market_time import as_cn_datetime, cn_now
        from app.services.strategy_input_manifest import normalize_request, required_minutes

        request = normalize_request(request)
        required_minutes(request, phase)
        now = cn_now()
        if (request["as_of"] != str(now.date())
                or as_cn_datetime(datetime.fromisoformat(request["bar_at"])) > now):
            raise ValueError("S02输入目标日期或分钟无效")
        key = common_key(request)
        from app.services.strategy_input_binding import algorithm_sha256
        if request["algorithm_sha256"] != algorithm_sha256():
            return {"state": "invalid_input", "key": key, "reason": "输入计算实现与当前算法摘要不一致"}
        # 重复轮询可读首次封存清单; 不基于后来发布的行情重新生成。
        existing = self.store.read_key(key, phase)
        if existing is not None:
            saved_status = self.store.path(key, phase).with_suffix(".status.json")
            status = self.status.get((key, phase), {})
            if not status and saved_status.exists():
                status = json.loads(saved_status.read_text())
            return {"state": "ready", "manifest": existing, "key": key,
                    "mirror_confirmed_at": status.get("mirror_confirmed_at")}
        if self.expired(request, phase):
            return {"state": "expired_input", "key": key, "reason": "实时输入接收期限已结束"}
        if not self.mirror and (not request["pool"].get("symbols")
                or request["selection"].get("as_of") != request["as_of"]
                or request["selection"].get("source_date") != request["reference_date"]
                or "rows" not in request["selection"]):
            return {"state": "waiting_input", "key": key, "reason": "等待精确日期预选归档"}
        job = (key, phase)
        with self.lock:
            if job not in self.status:
                status_path = self.store.path(key, phase).with_suffix(".status.json")
                if status_path.exists():
                    self.status[job] = json.loads(status_path.read_text())
            if not self.stopped and job not in self.active and time.monotonic() >= self.next_at.get(job, 0):
                self.active.add(job)
                self.pool.submit(self._advance, request, phase)
            return {"key": key, **self.status.get(job, {
                "state": "waiting_input", "reason": "输入未同步" if self.mirror else "等待必需输入"})}

    def _advance(self, request, phase):
        """Publisher采集/验证后封存; Receiver校验权威包, 迟到只留诊断。"""
        from app.market_time import cn_now
        from app.services.strategy_input_manifest import build_manifest
        key, job = common_key(request), (common_key(request), phase)
        try:
            if self.expired(request, phase):
                with self.lock:
                    self.status[job] = {**self.status.get(job, {}), "state": "expired_input",
                                        "reason": "实时输入接收期限已结束"}
                return
            if self.mirror:
                value = self.mirror.receive(self.store, key, phase,
                    accept=lambda: not self.stopped and not self.expired(request, phase))
                if self.expired(request, phase):
                    status = {"state": "expired_input", "reason": "输入晚于实时截止, 仅保存诊断证据"}
                elif value is None:
                    status = {"state": "waiting_input", "reason": "输入未同步"}
                else:
                    status = {"state": "ready", "mirror_confirmed_at": cn_now().isoformat()}
            else:
                baseline = self.store.baseline(key)
                if baseline:
                    request = {**deepcopy(request), "pool": baseline["pool"], "selection": baseline["selection"]}
                inputs = self._inputs(request, phase)
                value = build_manifest(request, phase=phase, published_at=cn_now().isoformat(), **inputs)
                status = {key: value[key] for key in (
                    "state", "coverage", "usable_symbols", "invalid_symbols")}
                if value["state"] == "ready":
                    # 采集及校验可能跨过截止, 必须在实际封存前再次检查。
                    if self.expired(request, phase):
                        status = {**status, "state": "expired_input", "reason": "输入晚于实时截止, 仅保存诊断证据"}
                        path = self.store.path(key, phase).with_suffix(".diagnostic.json")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_text(path, canonical({**value, "diagnostic_only": True}).decode())
                    elif not self.stopped:
                        self.store.seal(value)
                elif value["state"] == "waiting_input":
                    prior = self.status.get(job, {}).get("repairs", [])
                    repairs = {repair["id"]: repair for repair in prior + self._repair(request, phase, value)}
                    status["repairs"] = list(repairs.values())
                else:
                    status["reason"] = "完整成员中无有效输入"
            with self.lock:
                self.status[job] = status
                path = self.store.path(key, phase).with_suffix(".status.json")
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, canonical(status).decode())
        except Exception as exc:
            import httpx
            transient = isinstance(exc, (OSError, httpx.HTTPError))
            with self.lock:
                self.status[job] = {
                    **self.status.get(job, {}),
                    "state": "waiting_input" if self.mirror or transient else "invalid_input",
                    "reason": f"{'输入未同步' if self.mirror or transient else '输入证据异常'}: {exc}"}
                path = self.store.path(key, phase).with_suffix(".status.json")
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, canonical(self.status[job]).decode())
        finally:
            # 一次推进结束后至少3秒再试; 不是保证3秒内完成或重开已ready输入。
            with self.lock:
                self.active.discard(job)
                self.next_at[job] = time.monotonic() + 3

    def _inputs(self, request, phase):
        """从有租约的不可变公共发布读原始证据及必要分钟, 合并实际修复证明。

        非分钟世代可复用日线/因子核验缓存; 分钟每次读取当前绑定发布。
        首次基线存在后复用其池及预选, 广度昨收不能悄悄改写个股冻结参考。
        """
        from datetime import date

        import polars as pl

        from app.market_time import cn_now
        from app.multitenancy.public_reader import open_public_repository
        from app.multitenancy.publication import release_path
        from app.services.daily_close_validation import require_final_daily_rows
        from app.services.strategy_input_manifest import S01_ID, digest, required_minutes
        from app.services.strategy_input_source import load_source

        read_started = cn_now().isoformat()
        repo = open_public_repository(release_path(self.state.public_publisher.root), bounded=True)
        try:
            marker = repo.store._public_release_lease.validate()
            if request["strategy_id"] == S01_ID and not self.store.baseline(common_key(request)):
                missing = [row["symbol"] for row in request["selection"]["rows"]
                           if "reference_prior_ex_date" not in row]
                if missing:
                    from app.strategy.overnight_runtime import annotate_prior_ex_dates
                    flags = annotate_prior_ex_dates(pl.DataFrame({
                        "symbol": missing, "date": [date.fromisoformat(request["reference_date"])] * len(missing),
                    }), repo)
                    prior_ex = dict(zip(flags["symbol"], flags["prior_ex_date"], strict=True))
                    for row in request["selection"]["rows"]:
                        if "reference_prior_ex_date" not in row:
                            row["reference_prior_ex_date"] = prior_ex[row["symbol"]]
            source_key = digest([request, {key: value for key, value in marker["generations"].items()
                                          if "minute" not in key}])
            baseline = self.store.baseline(common_key(request))
            cached = self.source_cache.get(source_key)
            if cached is None:
                if baseline:
                    evidence = deepcopy(baseline["source_evidence"])
                    source_errors = evidence["source_errors"]
                else:
                    evidence, source_errors = load_source(repo, request)
                raw, _ = repo.get_daily_close_inputs(date.fromisoformat(request["reference_date"]),
                                                     include_closing_minute=False)
                if not raw.is_empty():
                    raw = raw.filter(pl.col("symbol").is_in(request["pool"]["symbols"]))
                if raw.is_empty():
                    references, daily_errors = {}, {}
                else:
                    daily_errors = require_final_daily_rows(
                        raw, date.fromisoformat(request["reference_date"]), isolate_symbols=True) or {}
                    valid = raw.filter(~pl.col("symbol").is_in(list(daily_errors)))
                    references = dict(zip(valid["symbol"].to_list(), valid["close"].to_list(), strict=True))
                if baseline and any(
                    row.get("reference_close") is not None and row["symbol"] in references
                    and row["reference_close"] != references[row["symbol"]]
                    for row in request["selection"]["rows"]):
                    raise ValueError("广度昨收与已封存个股原始昨收不一致")
                evidence["reference_daily"] = raw.to_dicts()
                evidence["reference_daily_sha256"] = digest(raw.to_dicts())
                evidence["reference_errors"] = {**source_errors, **daily_errors}
                evidence["daily_reference_errors"] = daily_errors
                cached = evidence, {**source_errors, **daily_errors}, references
                self.source_cache = {source_key: cached}
            evidence, errors, references = cached
            symbols = (request["pool"]["symbols"] if phase == "breadth"
                       else [row["symbol"] for row in request["selection"]["rows"]])
            required = required_minutes(request, phase)
            minutes = repo.get_minute_inputs(symbols, *minute_bounds(request, phase))
            from app.services.monitor_input_repair import merge_proof
            from app.services.strategy_input_binding import execution_environment
            coverage = {symbol: deepcopy(saved.get("proof", {})) for symbol, saved in
                        self.status.get((common_key(request), phase), {}).get("coverage", {}).items()}
            coverage = merge_proof(coverage, self.store.source_proof(common_key(request)))
            for repair in self.status.get((common_key(request), phase), {}).get("repairs", []):
                saved = self.state.monitor_input_repair._read(repair["id"])
                coverage = merge_proof(coverage, saved.get("result", {}).get("proof", {}))
            return dict(minutes=minutes, references=references, reference_errors=errors,
                        coverage=coverage, source_evidence=evidence, timing={
                            "input_read_started_at": read_started,
                            "input_read_finished_at": cn_now().isoformat(),
                            "last_required_bar_at": required[-1].isoformat(),
                            "public_release_id": marker["id"],
                            "collector_completed_at": marker.get("minute_completed_at"),
                            "priority_collector_completed_at": marker.get("minute_priority_completed_at"),
                            "market_publication_available_at": marker.get("published_at"),
                            "publisher_runtime": execution_environment(),
                        })
        finally:
            repo.store._public_release_finalizer()

    def _repair(self, request, phase, value):
        """仅为pending按相同缺分钟/日线依赖分组请求修复, 要求完整响应证明。"""
        from app.market_time import as_cn_datetime, cn_now
        from app.services.strategy_input_manifest import required_minutes
        key = common_key(request)
        pending = {symbol: state for symbol, state in value["coverage"].items() if state["state"] == "pending"}
        now = cn_now()
        deadline = as_cn_datetime(datetime.fromisoformat(request["retry_until"]))
        if now > deadline or (phase == "breadth" and now >= deadline):
            return []
        required = required_minutes(request, phase)
        groups = {}
        for symbol, state in pending.items():
            # 只修复缺失依赖; available、已证实不可用或非法成员不重复请求。
            dates = set(state.get("missing_daily_dates", []))
            if state["missing_reference"]:
                dates.add(request["reference_date"])
            dependency = (bool(state["missing_minutes"]), tuple(sorted(dates)))
            groups.setdefault(dependency, []).append(symbol)
        repairs = []
        for (minute, daily_dates), symbols in groups.items():
            repair = self.state.monitor_input_repair.request({
                "owner": f"s02-{key}", "strategy_id": request["strategy_id"],
                "generation": 1, "day": request["as_of"], "phase": phase,
                "attempt": 1, "symbols": sorted(symbols),
                "daily_dates": list(daily_dates),
                "minute_start": required[0].time().isoformat() if minute else None,
                "minute_end": required[-1].time().isoformat() if minute else None,
                "retry_until": request["retry_until"], "certify_coverage": True})
            if repair:
                repairs.append(repair)
        return repairs
