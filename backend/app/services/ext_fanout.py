"""Bounded catalog-to-member pulls with shared pacing and no partial publication."""
from __future__ import annotations

import asyncio
import copy
from contextlib import suppress
from datetime import date
from email.utils import parsedate_to_datetime
from time import time

import httpx

from app.services.ext_data import ExtConfig, FanoutConfig


class IncompletePullError(ValueError):
    def __init__(self, message: str, metadata: dict) -> None:
        super().__init__(message)
        self.metadata = metadata


def error_message(exc: Exception) -> str:
    """Return a bounded error without leaking authenticated request URLs."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"上游 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return f"上游网络错误 ({type(exc).__name__})"
    if isinstance(exc, (ValueError, TimeoutError)):
        return str(exc)[:200] or "拉取超时"
    return f"拉取失败 ({type(exc).__name__})"


class RequestBudget:
    def __init__(self, recipe: FanoutConfig):
        self.recipe = recipe
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def request(self, *args, **kwargs):
        from app.services.ext_pull import _request_json

        loop = asyncio.get_running_loop()
        for attempt in range(self.recipe.retries + 1):
            # Recheck shared cooldown after sleeping; never prevent an in-flight
            # 429 response from extending that cooldown by holding the lock.
            while True:
                async with self.lock:
                    delay = self.next_at - loop.time()
                    if delay <= 0:
                        self.next_at = loop.time() + self.recipe.min_interval_seconds
                        break
                await asyncio.sleep(delay)
            try:
                return await _request_json(*args, **kwargs)
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                code = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                terminal_4xx = code is not None and code != 429 and code < 500
                if attempt == self.recipe.retries or terminal_4xx:
                    raise

                delay = self.recipe.backoff_seconds * 2 ** attempt
                if isinstance(exc, httpx.HTTPStatusError):
                    retry_after = exc.response.headers.get("Retry-After", "")
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        with suppress(ValueError, TypeError, OverflowError):
                            delay = max(
                                delay,
                                parsedate_to_datetime(retry_after).timestamp() - time(),
                            )
                async with self.lock:
                    self.next_at = max(
                        self.next_at,
                        loop.time() + max(0.0, delay),
                    )


async def fetch_members(config: ExtConfig, day: date) -> tuple[list[dict], dict]:
    from app.services.ext_pull import _fetch_raw_pages

    recipe = FanoutConfig.model_validate(config.pull.fanout)
    budget = RequestBudget(recipe)
    metadata = {"total": 0, "completed": 0, "coverage": 0.0, "failed": []}
    unfinished: set[str] = set()
    try:
        async with asyncio.timeout(recipe.timeout_seconds):
            catalog = await _fetch_raw_pages(
                config,
                config.pull,
                day,
                request=budget.request,
                max_rows=recipe.max_items,
            )
            if not catalog:
                raise IncompletePullError("目录为空, 保留上一完整快照", metadata)

            ids = [str(row.get(recipe.id_field, "")) for row in catalog]
            if any(not key or len(key) > 128 for key in ids) or len(set(ids)) != len(ids):
                raise ValueError("目录 ID 缺失、重复或过长")

            metadata["total"] = len(catalog)
            unfinished.update(ids)
            results: list[list[dict] | None] = [None] * len(catalog)
            pending = iter(enumerate(catalog))
            row_count = 0

            async def worker():
                nonlocal row_count
                for index, directory in pending:
                    member = copy.copy(config.pull)
                    member.url = recipe.member_url
                    member.response_path = recipe.member_path
                    member.page_param = recipe.member_page_param
                    member.page_size_param = recipe.member_page_size_param
                    member.page_size = recipe.member_page_size
                    member.page_start = recipe.member_page_start
                    member.max_pages = recipe.member_max_pages
                    try:
                        if any(source not in directory for source in recipe.inject_fields):
                            raise ValueError("目录缺少注入字段")
                        rows = await _fetch_raw_pages(
                            config,
                            member,
                            day,
                            request=budget.request,
                            extra_params={recipe.member_param: ids[index]},
                            max_rows=recipe.max_rows,
                        )
                        row_count += len(rows)
                        if row_count > recipe.max_rows:
                            raise ValueError("两级拉取行数超过上限")
                        results[index] = [
                            {
                                **row,
                                **{
                                    target: directory[source]
                                    for source, target in recipe.inject_fields.items()
                                },
                            }
                            for row in rows
                        ]
                        metadata["completed"] += 1
                        unfinished.discard(ids[index])
                    except Exception as exc:
                        metadata["failed"].append({
                            "id": ids[index],
                            "reason": error_message(exc),
                        })
                        unfinished.discard(ids[index])

            async with asyncio.TaskGroup() as group:
                for _ in range(min(recipe.concurrency, len(catalog))):
                    group.create_task(worker())

            metadata["coverage"] = metadata["completed"] / metadata["total"]
            if metadata["failed"]:
                metadata["failed"].sort(key=lambda item: item["id"])
                raise IncompletePullError(
                    "两级拉取不完整, 已保留上一完整快照",
                    metadata,
                )
            return [row for rows in results for row in (rows or [])], metadata
    except TimeoutError as exc:
        metadata["failed"].extend(
            {"id": key, "reason": "整轮超时"}
            for key in sorted(unfinished)
        )
        metadata["coverage"] = (
            metadata["completed"] / metadata["total"]
            if metadata["total"]
            else 0.0
        )
        raise IncompletePullError(
            "两级拉取超时, 已保留上一完整快照",
            metadata,
        ) from exc


def join_members(rows: list[dict], recipe: dict) -> list[dict]:
    """Merge mapped labels in stable catalog order without dropping memberships."""
    if not recipe["join_fields"]:
        return rows

    key = recipe["group_by"]
    groups: dict[str, dict] = {}
    labels: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        identity = row.get(key)
        if not isinstance(identity, str) or not identity:
            raise ValueError("两级合并缺少有效分组字段")
        groups.setdefault(identity, dict(row))
        fields = labels.setdefault(
            identity,
            {field: [] for field in recipe["join_fields"]},
        )
        for field in fields:
            value = row.get(field)
            if value is None:
                raise ValueError("两级合并缺少分类字段")
            text = str(value)
            if text and text not in fields[field]:
                fields[field].append(text)
    for identity, row in groups.items():
        for field, separator in recipe["join_fields"].items():
            row[field] = separator.join(labels[identity][field])
    return list(groups.values())
