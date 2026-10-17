"""Two-level pulls, pagination completeness, and last-good snapshot retention."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import ext_pull
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig

DAY = date(2026, 10, 16)


def config(**kwargs):
    return ExtConfig(
        id="ext_gn_ths",
        label="概念",
        mode="snapshot",
        fields=[ExtField("symbol"), ExtField("所属概念")],
        pull=PullConfig(
            url="https://example.test/catalog",
            transform="none",
            **kwargs,
        ),
    )


def recipe(**kwargs):
    return {
        "member_url": "https://example.test/members",
        "id_field": "id",
        "member_param": "sector",
        "inject_fields": {"name": "所属概念"},
        "join_fields": {"所属概念": ";"},
        "concurrency": 2,
        "min_interval_seconds": 0,
        "retries": 2,
        "backoff_seconds": 0,
        **kwargs,
    }


async def test_fanout_retries_only_failed_members_and_merges_labels(monkeypatch):
    cfg = config(fanout=recipe())
    calls = []
    active = peak = 0

    async def request(pull, config_id, day=None, extra_params=None):
        nonlocal active, peak
        if pull.url.endswith("/catalog"):
            return [
                {"id": "a&x", "name": "AI"},
                {"id": "b", "name": "芯片"},
                {"id": "c", "name": "新能源"},
            ]
        key = extra_params["sector"]
        calls.append(key)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        if key == "b" and calls.count(key) == 1:
            raise httpx.HTTPStatusError(
                "secret URL",
                request=httpx.Request("GET", pull.url),
                response=httpx.Response(429, headers={"Retry-After": "0"}),
            )
        return [{"symbol": "600000.SH"}]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    rows = await ext_pull.fetch_rows_for_date(cfg, DAY)
    assert rows == [{"symbol": "600000.SH", "所属概念": "AI;芯片;新能源"}]
    assert calls.count("a&x") == calls.count("c") == 1
    assert calls.count("b") == 2
    assert peak <= 2


async def test_partial_failure_preserves_complete_data_and_state(
    monkeypatch,
    tmp_path,
):
    cfg = config(fanout=recipe())
    store = ExtConfigStore(tmp_path)
    store.upsert(cfg)
    failing = False

    async def request(pull, config_id, day=None, extra_params=None):
        if pull.url.endswith("/catalog"):
            return [{"id": "a", "name": "AI"}, {"id": "b", "name": "芯片"}]
        if failing and extra_params["sector"] == "b":
            raise httpx.ConnectError("https://secret.test/?token=do-not-expose")
        return [{"symbol": "600000.SH", "date": DAY.isoformat()}]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    part = tmp_path / "ext_data/ext_gn_ths/part.parquet"
    previous_bytes = part.read_bytes()
    completed = store.get(cfg.id).pull.last_complete

    failing = True
    with pytest.raises(ValueError, match="不完整"):
        await ext_pull.fetch_and_ingest(store.get(cfg.id), tmp_path, DAY)

    saved = store.get(cfg.id).pull
    assert part.read_bytes() == previous_bytes
    assert saved.last_complete == completed
    assert saved.last_status == "error"
    assert saved.last_attempt["completed"] == 1
    assert saved.last_attempt["total"] == 2
    assert saved.last_attempt["coverage"] == 0.5
    assert saved.last_attempt["failed"][0]["id"] == "b"
    assert "do-not-expose" not in str(saved.to_dict())


async def test_complete_replace_removes_stale_symbols_keeps_previous(
    monkeypatch,
    tmp_path,
):
    cfg = config()
    store = ExtConfigStore(tmp_path)
    store.upsert(cfg)
    batches = [
        [{"symbol": "600000.SH", "所属概念": "AI", "date": DAY.isoformat()}],
        [{"symbol": "000001.SZ", "所属概念": "芯片", "date": DAY.isoformat()}],
    ]

    async def request(*args, **kwargs):
        return batches.pop(0)

    monkeypatch.setattr(ext_pull, "_request_json", request)
    await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    first = store.get(cfg.id).pull.last_complete
    await ext_pull.fetch_and_ingest(store.get(cfg.id), tmp_path, DAY)

    base = tmp_path / "ext_data" / cfg.id
    assert pl.read_parquet(base / "part.parquet")["symbol"].to_list() == [
        "000001.SZ"
    ]
    assert pl.read_parquet(
        base / "previous-complete.parquet.bak"
    )["symbol"].to_list() == ["600000.SH"]
    assert [path.name for path in base.glob("*.parquet")] == ["part.parquet"]
    saved = store.get(cfg.id).pull
    assert saved.previous_complete == first
    assert saved.last_complete["source_date"] == DAY.isoformat()
    assert saved.last_complete["rows"] == 1
    assert len(saved.last_complete["sha256"]) == 64


async def test_source_date_unknown_not_replaced_with_receive_date(
    monkeypatch,
    tmp_path,
):
    cfg = config()
    ExtConfigStore(tmp_path).upsert(cfg)

    async def request(*args, **kwargs):
        return [{"symbol": "600000.SH", "所属概念": "AI"}]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    metadata = ExtConfigStore(tmp_path).get(cfg.id).pull.last_complete
    assert metadata["source_date"] is None
    assert metadata["received_at"]


async def test_wrong_date_after_twentieth_row_rejected_before_flatten(monkeypatch):
    cfg = config()
    cfg.pull.transform = "auto"

    async def request(*args, **kwargs):
        return [
            {"symbol": "600000.SH", "concepts": ["AI"], "date": DAY.isoformat()}
        ] * 20 + [
            {"symbol": "000001.SZ", "concepts": ["芯片"], "date": "2026-10-15"}
        ]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    with pytest.raises(ValueError, match="日期"):
        await ext_pull.fetch_rows_for_date(cfg, DAY)


async def test_pagination_cap_does_not_publish_partial_snapshot(
    monkeypatch,
    tmp_path,
):
    cfg = config(page_param="page", max_pages=1)
    ExtConfigStore(tmp_path).upsert(cfg)

    async def request(*args, **kwargs):
        return [{"symbol": "600000.SH", "所属概念": "AI"}]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    with pytest.raises(ValueError, match="上限"):
        await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    assert not (tmp_path / "ext_data/ext_gn_ths/part.parquet").exists()


async def test_config_change_during_request_cannot_publish_old_source(
    monkeypatch,
    tmp_path,
):
    cfg = config()
    store = ExtConfigStore(tmp_path)
    store.upsert(cfg)

    async def request(*args, **kwargs):
        latest = store.get(cfg.id)
        latest.pull.url = "https://example.test/new"
        store.upsert(latest)
        return [{"symbol": "600000.SH", "所属概念": "旧源"}]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    with pytest.raises(ValueError, match="配置已变更"):
        await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    assert store.get(cfg.id).pull.url.endswith("/new")
    assert not (tmp_path / "ext_data/ext_gn_ths/part.parquet").exists()


async def test_deleted_config_cannot_be_recreated_by_inflight_pull(
    monkeypatch,
    tmp_path,
):
    cfg = config()
    store = ExtConfigStore(tmp_path)
    store.upsert(cfg)

    async def request(*args, **kwargs):
        assert store.delete(cfg.id)
        return [{"symbol": "600000.SH", "所属概念": "旧源"}]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    with pytest.raises(ValueError, match="配置已删除"):
        await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    assert store.get(cfg.id) is None
    assert not (tmp_path / "ext_data/ext_gn_ths/part.parquet").exists()


async def test_fanout_cancel_releases_workers(monkeypatch):
    cfg = config(fanout=recipe())
    started = asyncio.Event()
    active = 0

    async def request(pull, config_id, day=None, extra_params=None):
        nonlocal active
        if pull.url.endswith("/catalog"):
            return [{"id": str(i), "name": str(i)} for i in range(10)]
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(ext_pull, "_request_json", request)
    task = asyncio.create_task(ext_pull.fetch_rows_for_date(cfg, DAY))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


def test_api_saves_recipe_and_preserves_omitted_new_fields(
    monkeypatch,
    tmp_path,
):
    from app.api.ext_data import router

    monkeypatch.setattr(ext_pull.pull_scheduler, "refresh", lambda *_: None)
    ExtConfigStore(tmp_path).upsert(config())
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)

    with TestClient(app) as client:
        payload = {
            "url": "https://example.test/catalog",
            "page_param": "p",
            "page_size_param": "size",
            "page_start": 0,
            "page_size": 50,
            "max_pages": 10,
            "transform": "none",
            "fanout": recipe(),
        }
        response = client.put("/api/ext-data/ext_gn_ths/pull", json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()["pull"]
        assert saved["page_param"] == "p"
        assert saved["page_start"] == 0
        assert saved["fanout"]["member_param"] == "sector"

        legacy = client.put(
            "/api/ext-data/ext_gn_ths/pull",
            json={"url": payload["url"]},
        )
        assert legacy.json()["pull"]["fanout"] == saved["fanout"]
        assert legacy.json()["pull"]["page_param"] == "p"

        invalid_recipes = (
            {"concurrency": 9},
            {"member_url": "https://other.test/members"},
        )
        for invalid in invalid_recipes:
            rejected = client.put(
                "/api/ext-data/ext_gn_ths/pull",
                json={**payload, "fanout": recipe(**invalid)},
            )
            assert rejected.status_code == 422


async def test_shared_request_encodes_member_id_and_applies_auth(monkeypatch):
    cfg = config(fanout=recipe())
    cfg.pull.auth = {"type": "query", "param": "token"}
    seen = []
    client_type = httpx.AsyncClient

    def respond(request):
        seen.append(request)
        assert request.url.params["token"] == "private+key"
        if request.url.path == "/catalog":
            return httpx.Response(
                200,
                json=[{"id": "a&admin=yes", "name": "AI"}],
            )
        assert request.url.params["sector"] == "a&admin=yes"
        assert "admin" not in request.url.params
        return httpx.Response(200, json=[{"symbol": "600000.SH"}])

    monkeypatch.setattr(
        "app.services.ext_data.get_ext_api_key",
        lambda _: "private+key",
    )
    monkeypatch.setattr(
        ext_pull.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    assert await ext_pull.fetch_rows_for_date(cfg, DAY) == [
        {"symbol": "600000.SH", "所属概念": "AI"}
    ]
    assert len(seen) == 2


async def test_source_structure_and_filtered_empty_cannot_replace(
    monkeypatch,
    tmp_path,
):
    cfg = config()
    store = ExtConfigStore(tmp_path)
    store.upsert(cfg)
    rows = [{"symbol": "600000.SH", "所属概念": "AI"}]

    async def request(*args, **kwargs):
        return rows

    monkeypatch.setattr(ext_pull, "_request_json", request)
    await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    path = tmp_path / "ext_data/ext_gn_ths/part.parquet"
    original = path.read_bytes()
    rows = [{"symbol": "430047.BJ", "所属概念": "AI"}]
    with pytest.raises(ValueError, match="有效数据"):
        await ext_pull.fetch_and_ingest(store.get(cfg.id), tmp_path, DAY)
    assert path.read_bytes() == original
    changed = store.get(cfg.id)
    changed.pull.transform = "auto"
    store.upsert(changed)
    with pytest.raises(ValueError, match="来源结构"):
        await ext_pull.fetch_and_ingest(changed, tmp_path, DAY)
    assert path.read_bytes() == original


async def test_local_stock_coverage_is_separate_from_member_completion(
    monkeypatch,
    tmp_path,
):
    cfg = config()
    ExtConfigStore(tmp_path).upsert(cfg)
    instruments = tmp_path / "instruments/instruments.parquet"
    instruments.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"]}).write_parquet(
        instruments
    )

    async def request(*args, **kwargs):
        return [{"symbol": "600000.SH", "所属概念": "AI"}]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    metadata = ExtConfigStore(tmp_path).get(cfg.id).pull.last_complete
    assert metadata["coverage"] == 1.0
    assert metadata["stock_coverage"] == {
        "matched": 1,
        "total": 2,
        "ratio": 0.5,
    }


async def test_concurrent_pull_rejected_and_cancel_allows_next_run(
    monkeypatch,
    tmp_path,
):
    cfg = config()
    ExtConfigStore(tmp_path).upsert(cfg)
    started = asyncio.Event()

    async def request(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ext_pull, "_request_json", request)
    first = asyncio.create_task(ext_pull.fetch_and_ingest(cfg, tmp_path, DAY))
    await started.wait()
    with pytest.raises(ValueError, match="正在拉取"):
        await ext_pull.fetch_and_ingest(cfg, tmp_path, DAY)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not ext_pull._ACTIVE_PULLS


async def test_whole_run_timeout_records_inflight_and_unstarted_members(
    monkeypatch,
):
    cfg = config(fanout=recipe(timeout_seconds=1, concurrency=1))

    async def request(pull, *args, **kwargs):
        if pull.url.endswith("/catalog"):
            return [{"id": key, "name": key} for key in ("a", "b", "c")]
        await asyncio.Event().wait()

    monkeypatch.setattr(ext_pull, "_request_json", request)
    with pytest.raises(ValueError, match="超时") as raised:
        await ext_pull.fetch_rows_for_date(cfg, DAY)
    assert [item["id"] for item in raised.value.metadata["failed"]] == [
        "a",
        "b",
        "c",
    ]
    assert raised.value.metadata["coverage"] == 0
