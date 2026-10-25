"""Durable model-call evidence and conservative budget admission.

Unknown usage/prices remain unknown. Tenant counters use the existing binding
transaction; standalone has one file writer. Neither prompts nor secrets enter
the ledger. Reservations survive crashes until the daily budget rolls over.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

from app.config import settings
from app.services.fs_utils import atomic_write_text
from app.services.observability import current_request_id

_scope: ContextVar[object | None] = ContextVar("ai_usage_documents", default=None)
_file_lock = threading.RLock()


@contextmanager
def ai_usage_scope(documents):
    token = _scope.set(documents)
    try:
        yield
    finally:
        _scope.reset(token)


def _path():
    root = settings.platform_config_dir or settings.data_dir / "user_data"
    return root / "ai_usage.json"


def _local_allowed():
    if settings.multitenancy_enabled and settings.service_role not in {"standalone", "platform"}:
        raise RuntimeError("AI 用量缺少租户上下文")


def _read():
    documents = _scope.get()
    if documents is not None:
        return documents.get("ai_usage", "ledger", {})
    _local_allowed()
    path = _path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid ledger")
        return value
    except (OSError, ValueError) as exc:
        raise RuntimeError("AI 用量记录不可读, 请修复后重试") from exc


def _mutate(change):
    documents = _scope.get()
    if documents is not None:
        return documents.mutate("ai_usage", "ledger", lambda value: change(value or {}))
    _local_allowed()
    with _file_lock:
        value = change(_read())
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False), mode=0o600)
        return value


def _user():
    documents = _scope.get()
    return documents.context.user_id if documents is not None else "standalone"


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _tokens(value):
    """Only accept complete upstream integer counts; missing is not zero."""
    incoming = _field(value, "input_tokens")
    outgoing = _field(value, "output_tokens")
    if incoming is None:
        incoming = _field(value, "prompt_tokens")
    if outgoing is None:
        outgoing = _field(value, "completion_tokens")
    if any(type(count) is not int or count < 0 for count in (incoming, outgoing)):
        return None
    return {"input_tokens": incoming, "output_tokens": outgoing, "total_tokens": incoming + outgoing}


def _price(provider, model):
    from app.services.ai_provider import normalize_openai_base_url

    try:
        item = json.loads(settings.ai_pricing_json).get(model)
        if not isinstance(item, dict) or item.get("provider") != provider:
            return None
        from app import secrets_store

        base = secrets_store.get_ai_config("ai_base_url", settings.ai_base_url)
        if normalize_openai_base_url(item.get("base_url", "")) != normalize_openai_base_url(base):
            return None
        if item.get("currency") != settings.ai_budget_currency or not item.get("source"):
            return None
        rates = [Decimal(str(item[key])) for key in ("input_per_million", "output_per_million")]
        if any(not rate.is_finite() or rate < 0 for rate in rates):
            return None
        return {"input": rates[0], "output": rates[1], "source": str(item["source"])[:256],
                "currency": item["currency"]}
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        return None


def _cost(tokens, price):
    if tokens is None or price is None:
        return None
    # Tokens / million * price * million currency micros.
    return int((tokens["input_tokens"] * price["input"]
                + tokens["output_tokens"] * price["output"]).to_integral_value(rounding=ROUND_CEILING))


def output_cap(max_tokens):
    """A token/cost budget needs an explicit upstream output bound."""
    if settings.ai_daily_token_limit or settings.ai_daily_cost_limit_micros:
        return min(max_tokens or settings.ai_max_output_tokens, settings.ai_max_output_tokens)
    return max_tokens


class Reservation:
    def __init__(self, provider, model, protocol, max_tokens):
        self.id = uuid.uuid4().hex
        self.day = datetime.now(UTC).date().isoformat()
        self.price = _price(provider, model)
        self.user = _user()
        self.task = current_request_id()
        if self.task == "-":
            self.task = self.id
        financial = bool(settings.ai_daily_token_limit or settings.ai_daily_cost_limit_micros)
        if financial and protocol == "codex_cli":
            raise RuntimeError("当前 CLI 无可核验的 Token 上限, 无法启用 Token/费用预算")
        if settings.ai_daily_cost_limit_micros and self.price is None:
            raise RuntimeError("AI 费用预算缺少匹配的模型价格、币种或来源")
        from app.services.ai_provider import current_ai_context_window

        context_window = current_ai_context_window()
        reserve_tokens = context_window + (max_tokens or settings.ai_max_output_tokens) if financial else 0
        self.record = {
            "id": self.id, "request_id": current_request_id(), "user_id": self.user,
            "provider": provider, "model": model, "protocol": protocol,
            "started_at": datetime.now(UTC).isoformat(), "status": "pending",
            "usage": None, "cost_micros": None, "price_source": self.price["source"] if self.price else None,
            "currency": self.price["currency"] if self.price else None,
            "reserved_tokens": reserve_tokens,
            "reserved_cost_micros": _cost(
                {"input_tokens": context_window,
                 "output_tokens": max_tokens or settings.ai_max_output_tokens}, self.price,
            ) if financial else 0,
        }
        self.done = False
        _mutate(self._reserve)

    def _reserve(self, ledger):
        days = ledger.setdefault("days", {})
        day = days.setdefault(self.day, {"calls": 0, "users": {}, "tasks": {},
                                        "known_tokens": 0, "known_cost_micros": 0,
                                        "unknown_usage": 0, "unknown_cost": 0, "pending": {},
                                        "currency": settings.ai_budget_currency})
        if "currency" not in day and not day["known_cost_micros"]:
            day["currency"] = settings.ai_budget_currency
        if day["calls"] and day.get("currency") != settings.ai_budget_currency:
            raise RuntimeError("AI 预算币种已变更, 请等待次日或核对用量记录")
        for count, limit in [
            (day["calls"], settings.ai_daily_request_limit),
            (day["users"].get(self.user, 0), settings.ai_user_daily_request_limit),
            (day["tasks"].get(self.task, 0), settings.ai_task_request_limit),
        ]:
            if limit and count >= limit:
                raise RuntimeError("AI 调用次数已达到配置预算")
        if settings.ai_daily_token_limit:
            total = day["known_tokens"] + sum(r["reserved_tokens"] for r in day["pending"].values())
            if (day["unknown_usage"] or any(not r["reserved_tokens"] for r in day["pending"].values())
                    or total + self.record["reserved_tokens"] > settings.ai_daily_token_limit):
                raise RuntimeError("AI Token 预算不足或存在未知用量")
        if settings.ai_daily_cost_limit_micros:
            if any(r["reserved_cost_micros"] is None or not r["reserved_tokens"] for r in day["pending"].values()):
                raise RuntimeError("AI 费用预算存在未预留的调用")
            total = day["known_cost_micros"] + sum(r["reserved_cost_micros"] for r in day["pending"].values())
            if day["unknown_cost"] or total + self.record["reserved_cost_micros"] > settings.ai_daily_cost_limit_micros:
                raise RuntimeError("AI 费用预算不足或存在未知费用")
        if (len(day["pending"]) >= 128
                or (len(day["tasks"]) >= 2000 and self.task not in day["tasks"])
                or (len(day["users"]) >= 1000 and self.user not in day["users"])):
            raise RuntimeError("AI 用量记录容量已达到上限")
        day["calls"] += 1
        day["users"][self.user] = day["users"].get(self.user, 0) + 1
        day["tasks"][self.task] = day["tasks"].get(self.task, 0) + 1
        day["pending"][self.id] = self.record
        for old in sorted(days)[:-7]:
            del days[old]
        return ledger

    def finish(self, status, usage=None, upstream_id=None, error=None):
        if self.done:
            return
        counts = _tokens(usage)
        cost = _cost(counts, self.price)
        record = {**self.record, "status": status, "usage": counts, "cost_micros": cost,
                  "upstream_id": str(upstream_id)[:256] if upstream_id else None,
                  "failure_kind": type(error).__name__ if error is not None else None,
                  "finished_at": datetime.now(UTC).isoformat()}

        def update(ledger):
            day = ledger.get("days", {}).get(self.day)
            if day is None or self.id not in day["pending"]:
                raise RuntimeError("AI 用量预留记录丢失")
            del day["pending"][self.id]
            day["known_tokens"] += counts["total_tokens"] if counts else 0
            day["known_cost_micros"] += cost if cost is not None else 0
            day["unknown_usage"] += counts is None
            day["unknown_cost"] += cost is None
            ledger["records"] = [*ledger.get("records", [])[-99:], record]
            return ledger

        _mutate(update)
        self.done = True


async def close_ai_resource(value):
    close = getattr(value, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


class UsageStream:
    def __init__(self, stream, reservation):
        self.stream, self.reservation = stream, reservation
        self.usage, self.upstream_id = None, None
        self.status = "cancelled"

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                response = _field(chunk, "response") or chunk
                self.usage = _field(response, "usage") or self.usage
                self.upstream_id = _field(response, "_request_id") or _field(response, "id") or self.upstream_id
                kind = _field(chunk, "type")
                if kind in {"response.completed", "response.failed", "response.incomplete"}:
                    self.status = kind.split(".")[1]
                    await asyncio.to_thread(self.reservation.finish, self.status, self.usage, self.upstream_id)
                yield chunk
            self.status = "completed"
        except BaseException as exc:
            await asyncio.to_thread(
                self.reservation.finish, "cancelled" if isinstance(
                    exc, (asyncio.CancelledError, GeneratorExit),
                ) else "failed",
                self.usage, self.upstream_id, exc,
            )
            raise
        finally:
            await self.close()

    async def close(self):
        try:
            await close_ai_resource(self.stream)
        finally:
            await asyncio.to_thread(self.reservation.finish, self.status, self.usage, self.upstream_id)


async def metered_create(create, *, protocol, **kwargs):
    from app.services.ai_provider import current_ai_provider

    reservation = await asyncio.to_thread(
        Reservation, current_ai_provider(), kwargs["model"], protocol,
        kwargs.get("max_tokens", kwargs.get("max_output_tokens")),
    )
    try:
        response = await create(**kwargs)
        if kwargs.get("stream"):
            return UsageStream(response, reservation)
        await asyncio.to_thread(
            reservation.finish, _field(response, "status") or "completed",
            _field(response, "usage"), _field(response, "_request_id") or _field(response, "id"),
        )
        return response
    except BaseException as exc:
        await asyncio.to_thread(
            reservation.finish, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=exc,
        )
        raise


async def metered_codex(execute):
    from app.services.ai_provider import current_ai_model, current_ai_provider

    reservation = await asyncio.to_thread(Reservation, current_ai_provider(), current_ai_model(), "codex_cli", None)
    try:
        result = await execute()
        await asyncio.to_thread(reservation.finish, "completed")
        return result
    except BaseException as exc:
        await asyncio.to_thread(reservation.finish, "failed", error=exc)
        raise


def usage_summary():
    with _file_lock:
        ledger = _read()
    day = datetime.now(UTC).date().isoformat()
    counters = ledger.get("days", {}).get(day, {})
    records = [r for r in ledger.get("records", []) if r["user_id"] == _user()]
    return {
        "day_utc": day, "user_calls": counters.get("users", {}).get(_user(), 0),
        "tenant_calls": counters.get("calls", 0),
        "known_tokens": counters.get("known_tokens", 0),
        "known_cost_micros": counters.get("known_cost_micros", 0),
        "unknown_usage_calls": counters.get("unknown_usage", 0),
        "unknown_cost_calls": counters.get("unknown_cost", 0),
        "pending_calls": len(counters.get("pending", {})), "currency": settings.ai_budget_currency,
        "records": records,
        "limits": {name: getattr(settings, f"ai_{name}") for name in (
            "daily_request_limit", "user_daily_request_limit", "task_request_limit",
            "daily_token_limit", "daily_cost_limit_micros",
        )},
    }
