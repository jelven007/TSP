"""Independent, resumable discovery of newly announced halt/resume events."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.market_time import cn_now
from app.services import pipeline_jobs, preferences
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)
_lock = threading.Lock()


class SyncInterruptedError(Exception):
    """Shutdown or temporary automatic-mode interruption, resumable on return."""


def _load(repo):
    path = repo.store.data_dir / "suspension_sync" / "current.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or not all(isinstance(state.get(key), list) for key in ("universe", "pending", "verified"))
            or not all(isinstance(state.get(key), dict) for key in ("feeds", "errors", "scans"))
            or not state["universe"] or len(set(state["universe"])) != len(state["universe"])
            or any(not isinstance(s, str) or not s.endswith((".SH", ".SZ")) for s in state["universe"])
            or any(not isinstance(key, str) for key in state["verified"])
            or state.get("status") not in {"running", "queued", "succeeded", "failed", "cancelled", "interrupted"}
            or type(state.get("attempts")) is not int or state["attempts"] < 0
            or type(state.get("added")) is not int or state["added"] < 0
            or type(state.get("publish_pending")) is not bool):
        raise ValueError("停复牌自动同步状态无效")
    date.fromisoformat(state["day"])
    if datetime.fromisoformat(state["cutoff"]).tzinfo is None:
        raise ValueError("停复牌自动同步观察时间缺少时区")
    if datetime.fromisoformat(state["cutoff"]) > cn_now():
        raise ValueError("停复牌自动同步观察时间晚于当前时钟")
    if state.get("next_retry_at") and datetime.fromisoformat(state["next_retry_at"]).tzinfo is None:
        raise ValueError("停复牌自动同步重试时间缺少时区")
    retries = state.setdefault("notice_retries", {})
    if (not isinstance(retries, dict) or type(state.get("failure_streak", 0)) is not int
            or state.get("failure_streak", 0) < 0):
        raise ValueError("停复牌重试状态无效")
    for key, retry in retries.items():
        if (not isinstance(key, str) or not isinstance(retry, dict)
                or not isinstance(retry.get("fingerprint"), str)
                or retry.get("category") not in {"semantic", "transport"}
                or type(retry.get("attempts")) is not int or retry["attempts"] < 1
                or datetime.fromisoformat(retry["next_retry_at"]).tzinfo is None):
            raise ValueError("停复牌逐公告重试状态无效")
    for exchange, feed in state["feeds"].items():
        if (exchange not in {"SH", "SZ"} or not isinstance(feed, dict)
                or type(feed.get("pages")) is not int or not 1 <= feed["pages"] <= 64
                or type(feed.get("rows")) is not int
                or not 0 <= feed["rows"] <= 64 * (100 if exchange == "SH" else 50)):
            raise ValueError("停复牌自动同步市场覆盖无效")
    for notice in state["pending"]:
        if (not isinstance(notice, dict) or notice.get("symbol") not in state["universe"]
                or any(not isinstance(notice.get(k), str) for k in ("title", "url", "formal_date"))):
            raise ValueError("停复牌自动同步公告队列无效")
        date.fromisoformat(notice["formal_date"])
    for exchange, scan in state["scans"].items():
        if (exchange not in {"SH", "SZ"} or not isinstance(scan, dict)
                or type(scan.get("page")) is not int or not 1 <= scan["page"] <= 64
                or type(scan.get("total")) is not int or not 0 <= scan["total"] <= 6400
                or not all(isinstance(scan.get(k), list) for k in ("seen", "first_keys", "notices"))):
            raise ValueError("停复牌自动同步分页断点无效")
        date.fromisoformat(scan["begin"])
        date.fromisoformat(scan["end"])
    return state


def _save(repo, state):
    root = repo.store.data_dir / "suspension_sync"
    root.mkdir(parents=True, exist_ok=True)
    state = {**state, "updated_at": cn_now().isoformat()}
    text = json.dumps(state, ensure_ascii=False, allow_nan=False)
    atomic_write_text(root / f'{state["day"]}.json', text)
    atomic_write_text(root / "current.json", text)
    return state


def _key(notice):
    return f'{notice["symbol"]}|{notice["formal_date"]}|{notice["url"]}'

def _fingerprint(notice):
    from app.data_providers import suspension_announcements
    from app.services import issuer_st_status

    # A parser change or revised list metadata makes the pending evidence eligible
    # immediately. A bounded later retry also detects changed content at the same URL.
    return hashlib.sha256(
        Path(suspension_announcements.__file__).read_bytes()
        + Path(issuer_st_status.__file__).read_bytes()
        + json.dumps(notice, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _notice_retry(state, notice, category):
    key = _key(notice)
    previous = state["notice_retries"].get(key, {})
    fingerprint = _fingerprint(notice)
    attempts = (previous.get("attempts", 0) if previous.get("fingerprint") == fingerprint else 0) + 1
    delay = min(1800 if category == "semantic" else 900,
                (900 if category == "semantic" else 60) * 2 ** min(attempts - 1, 5))
    state["notice_retries"][key] = {
        "fingerprint": fingerprint, "category": category, "attempts": attempts,
        "next_retry_at": (cn_now() + timedelta(seconds=delay)).isoformat(),
    }


def _coverage(state):
    unscanned = {s for s in state["universe"] if s.split(".")[1] not in state["feeds"]}
    unresolved = {n["symbol"] for n in state["pending"]}
    pending = unscanned | unresolved
    return {"universe_count": len(state["universe"]),
            "covered_count": len(set(state["universe"]) - pending),
            "pending_count": len(pending), "pending_symbols": sorted(pending),
            "pending_notices": len(state["pending"]),
            "unscanned_count": len(unscanned), "unresolved_count": len(unresolved),
            "completion": "complete" if not pending and not state["errors"] and not state["publish_pending"]
                          else "partial_verified" if set(state["universe"]) - pending else "unverified",
            "coverage_complete": (not pending and len(state["feeds"]) == 2
                                  and not state["errors"] and not state["publish_pending"]),
            "coverage_basis": "complete_official_market_lists"}


def summary(repo):
    try:
        state = _load(repo)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "failed", "error": str(exc), "coverage_complete": False}
    if state is None:
        return {"status": "not_started", "coverage_complete": False}
    fields = ("day", "cutoff", "updated_at", "status", "attempts", "added",
              "errors", "next_retry_at", "publish_pending", "data_release_id", "public_release_id",
              "failure_streak", "notice_retries", "evidence_policy",
              "budget_paused_phase", "market_attempts")
    return {**{key: state.get(key) for key in fields}, **_coverage(state),
            "feeds": state["feeds"],
            "partial_pages": {market: scan["page"] for market, scan in state["scans"].items()}}


def _publish(repo, publisher):
    from app.services.data_release import publish_data_release
    from app.services.public_query_cache import invalidate_data_cache, invalidate_overview_cache
    from app.services.screener import ScreenerService

    # Repository readers detect atomic replacement; the cached history inputs
    # and public immutable release also have to observe the same new evidence.
    repo.get_suspension_history()
    ScreenerService.clear_history_cache()
    invalidate_data_cache("instruments")
    invalidate_overview_cache()
    release = publish_data_release(repo.store.data_dir, reason="suspension_sync",
                                   write_lock=getattr(repo, "_write_lock", None))
    if settings.service_role == "platform" and publisher is None:
        raise ValueError("停复牌证据已保存, 公共发布器尚未就绪")
    public = publisher.publish() if publisher is not None else None
    return {"data_release_id": release["release_id"], "public_release_id": (public or {}).get("id")}


def request_sync(repo, publisher=None, *, stop=None, lookup=None, max_notices=32, refresh=False):
    """Small tracked job; daily-price gaps and heavy pipeline slots are irrelevant."""
    import httpx

    from app.data_providers.suspension_announcements import (
        AnnouncementBudgetError,
        AnnouncementLookup,
    )
    from app.services.announced_stock_status import merge_risk_events
    from app.services.instrument_sync import load_current_stock_pool
    from app.services.selection_suspension_repair import merge_events

    now = cn_now()
    if (settings.service_role not in {"platform", "standalone"}
            or not preferences.get_data_sync_auto_enabled()
            or (stop is not None and stop.is_set()) or not _lock.acquire(blocking=False)):
        return None
    try:
        state, state_error = None, None
        try:
            state = _load(repo)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            state_error = exc
        if state and state["status"] == "cancelled" and state["day"] == str(now.date()):
            return state
        if (state and not refresh and state.get("evidence_policy") == "stock_status_v2"
                and state.get("next_retry_at")
                and datetime.fromisoformat(state["next_retry_at"]) > now):
            return state
        store = pipeline_jobs.job_store
        job_id, created = store.create(kind="suspension_sync", source="scheduled",
                                      request={"purpose": "suspension_sync", "day": str(now.date())})
        if not created:
            return state

        def check():
            if pipeline_jobs.is_cancelled(job_id):
                raise pipeline_jobs.JobCancelledError(job_id)
            if (stop is not None and stop.is_set()) or not preferences.get_data_sync_auto_enabled():
                raise SyncInterruptedError("停复牌自动同步暂时中断")

        owned = lookup is None
        try:
            store.start(job_id)
            if state_error is not None:
                raise ValueError(f"停复牌自动同步状态不可读取: {state_error}")
            check()
            universe = sorted(load_current_stock_pool(repo.store.data_dir).symbols)
            if not universe:
                raise ValueError("完整股票目录为空, 不能认证停复牌覆盖")
            if (state is None or state["day"] != str(now.date()) or state["universe"] != universe
                    or state["status"] == "succeeded" or refresh
                    or state.get("evidence_policy") != "stock_status_v2"):
                previous = state or {}
                state = {"schema_version": 1, "day": str(now.date()), "cutoff": now.isoformat(),
                         "universe": universe, "feeds": {}, "scans": {},
                         "pending": [n for n in previous.get("pending", []) if n["symbol"] in universe],
                         "errors": {}, "verified": previous.get("verified", []) if previous.get("day") == str(now.date()) else [],
                         "added": 0, "attempts": 0,
                         "notice_retries": {k: v for k, v in previous.get("notice_retries", {}).items()
                                            if k in {_key(n) for n in previous.get("pending", [])
                                                     if n["symbol"] in universe}},
                         "failure_streak": 0,
                         "evidence_policy": "stock_status_v2",
                         "publish_pending": bool(previous.get("publish_pending"))}
            elif (len(state["feeds"]) == 2
                  and now - datetime.fromisoformat(state["cutoff"]) >= timedelta(minutes=5)):
                # Old unresolved PDFs must not stop discovery of newer events.
                # An unfinished sweep keeps its original window: the provider
                # rechecks page 1 on resume and rejects a changed list.
                state = {**state, "cutoff": now.isoformat(), "feeds": {}, "scans": {},
                         "market_attempts": {}}
            state = {**state, "status": "running", "job_id": job_id,
                     "attempts": state["attempts"] + 1, "next_retry_at": None, "error": None,
                     "market_attempts": state.get("market_attempts") or {}, "budget_paused_phase": None}
            state = _save(repo, state)
            lookup = lookup or AnnouncementLookup(check_cancelled=check)
            cutoff = datetime.fromisoformat(state["cutoff"])
            # Include tomorrow's formal dates: notices often go public the night
            # before the date printed on the exchange list.
            begin, end = cutoff.date() - timedelta(days=3), now.date() + timedelta(days=1)
            for exchange in ("SH", "SZ"):
                check()
                if exchange in state["feeds"]:
                    continue
                store.progress(job_id, "scan_announcements", 5, f"{exchange}: 完整扫描停复牌公告")
                resumed_from = (state["scans"].get(exchange) or {}).get("page")
                market = {"started_at": cn_now().isoformat(), "resumed_after_page": resumed_from}
                try:
                    def save_page(checkpoint, market=exchange):
                        nonlocal state
                        check()
                        if checkpoint is None:
                            state["scans"].pop(market, None)
                        else:
                            state["scans"][market] = checkpoint
                        state = _save(repo, state)

                    feed = lookup.market_notices(
                        exchange, begin, end, checkpoint=state["scans"].get(exchange),
                        on_progress=save_page,
                    )
                    check()
                    known = {_key(n) for n in state["pending"]} | set(state["verified"])
                    refreshed = {_key(n): n for n in feed["notices"]}
                    state["pending"] = [refreshed.get(_key(n), n) for n in state["pending"]]
                    relevant = [n for n in feed["notices"] if n["symbol"] in universe and _key(n) not in known]
                    state["pending"].extend(relevant)
                    state["feeds"][exchange] = {"pages": feed["pages"], "rows": feed["rows"]}
                    state["scans"].pop(exchange, None)
                    state["errors"].pop(exchange, None)
                    state["market_attempts"][exchange] = {
                        **market, "state": "complete", "last_page": feed["pages"],
                        "finished_at": cn_now().isoformat(),
                    }
                    state = _save(repo, state)
                except (pipeline_jobs.JobCancelledError, SyncInterruptedError):
                    raise
                except Exception as exc:
                    check()
                    if isinstance(exc, AnnouncementBudgetError):
                        reason = "attempt_budget"
                        state["budget_paused_phase"] = f"scan_{exchange}"
                        # This market is incomplete, not a transport failure.
                        state["errors"].pop(exchange, None)
                    else:
                        reason = ("read_timeout" if isinstance(exc, httpx.ReadTimeout) else
                                  "transport" if isinstance(exc, (httpx.RequestError, TimeoutError)) else
                                  "http_status" if isinstance(exc, httpx.HTTPStatusError) else "invalid_evidence")
                        state["errors"][exchange] = str(exc)
                    state["market_attempts"][exchange] = {
                        **market, "state": "incomplete", "reason": reason,
                        "last_page": (state["scans"].get(exchange) or {}).get("page"),
                        "finished_at": cn_now().isoformat(),
                    }
                    state = _save(repo, state)

            # Failed notices rotate behind unattempted ones so a broken PDF
            # cannot starve newly announced events later in the feed.
            batch = [n for n in state["pending"]
                     if not (retry := state["notice_retries"].get(_key(n)))
                     or retry["fingerprint"] != _fingerprint(n)
                     or datetime.fromisoformat(retry["next_retry_at"]) <= cn_now()][:max_notices]
            for index, notice in enumerate(batch):
                check()
                key = _key(notice)
                store.progress(job_id, "verify_suspensions", 10 + int(70 * index / max(1, len(batch))),
                               f'核验{notice["symbol"]}: {notice["title"]}')
                try:
                    response = lookup.verify_notice(notice, cutoff=cutoff)
                    check()
                    events = response["events"]
                    risk_events = response.get("risk_warning_events", [])
                    wall = cutoff.replace(tzinfo=None)
                    if any(e["symbol"] != notice["symbol"] or e.get("announced_at") is None
                           or e["announced_at"] > wall for e in [*events, *risk_events]):
                        raise ValueError("公告返回证券或公开时钟与本轮不符")
                    if events or risk_events:
                        # Journal before writing; interruption cannot lose the
                        # need to publish evidence already merged into Parquet.
                        publish_pending = state["publish_pending"]
                        state["publish_pending"] = True
                        state = _save(repo, state)
                        added = merge_events(repo, events)
                        added += merge_risk_events(repo, risk_events)
                        state["added"] += added
                        state["publish_pending"] = publish_pending or bool(added)
                    state["pending"].remove(notice)
                    if response.get("diagnostics"):
                        state["pending"].append(notice)
                        state["errors"][key] = response["diagnostics"]
                        _notice_retry(state, notice, response.get("retry_category", "transport"))
                    else:
                        state["errors"].pop(key, None)
                        state["notice_retries"].pop(key, None)
                        if events or risk_events or not response.get("deferred"):
                            state["verified"].append(key)
                except (pipeline_jobs.JobCancelledError, SyncInterruptedError):
                    raise
                except AnnouncementBudgetError:
                    state["budget_paused_phase"] = "verify_notices"
                    break
                except Exception as exc:
                    check()
                    state["errors"][key] = str(exc)
                    _notice_retry(state, notice, "transport")
                    state["pending"].remove(notice)
                    state["pending"].append(notice)
                state = _save(repo, state)
            check()
            if state.get("publish_pending"):
                store.progress(job_id, "publish_suspensions", 90, "发布已查证停复牌证据")
                state.update(_publish(repo, publisher))
                state["publish_pending"] = False
            coverage = _coverage(state)
            if state["errors"]:
                raise ValueError("停复牌查证未完成, 保留覆盖与逐股异常并继续重试")
            state["status"] = "succeeded" if coverage["coverage_complete"] else "queued"
            state["failure_streak"] = 0
            state["next_retry_at"] = (cn_now() + timedelta(
                seconds=300 if state["status"] == "succeeded" else 30)).isoformat()
            state = _save(repo, state)
            store.succeed(job_id, {**summary(repo), "feeds": state["feeds"]})
            return state
        except SyncInterruptedError as exc:
            store.fail(job_id, str(exc))
            if state is not None:
                state = _save(repo, {**state, "status": "interrupted", "next_retry_at": None})
            return state
        except pipeline_jobs.JobCancelledError:
            store.cancelled(job_id)
            if state is not None:
                state = _save(repo, {**state, "status": "cancelled"})
            return state
        except Exception as exc:
            logger.warning("停复牌自动同步未完成: %s", exc)
            store.fail(job_id, str(exc))
            if state is None:
                # No trustworthy directory: leave a visible job failure, not
                # a fictitious successful sweep over an empty universe.
                return {"status": "failed", "error": str(exc), "coverage_complete": False}
            streak = state.get("failure_streak", 0) + 1
            delay = min(900, 60 * 2 ** min(streak - 1, 5))
            if not state.get("publish_pending") and not any(k in state["errors"] for k in ("SH", "SZ")):
                # Continue discovery at least every five minutes. Old semantic
                # failures remain pending, without re-downloading every minute.
                delay = min(delay, 300)
            state = _save(repo, {**state, "status": "failed", "error": str(exc),
                                 "failure_streak": streak,
                                 "next_retry_at": (cn_now() + timedelta(seconds=delay)).isoformat()})
            return state
        finally:
            if owned and lookup is not None:
                lookup.close()
    finally:
        _lock.release()
