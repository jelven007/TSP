"""Full AI contracts: real tenant SQL, original tools, and revoked sessions."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.custom.assistant import actions, chat_service, tools
from app.multitenancy import access
from app.multitenancy.documents import TenantDocuments
from app.services.json_report_store import JsonReportStore


def documents(env, user):
    return TenantDocuments(env["db"], env["identity"].context(user, None, "ai-test"))


def test_private_reports_concurrent_retention_and_no_global_files(env, monkeypatch):
    _, first = register(env)
    _, second = register(env)
    own, other = documents(env, first), documents(env, second)
    store = JsonReportStore("ai_reports.json", 20, "rpt")
    monkeypatch.setattr(store, "_path", lambda: pytest.fail("global report path"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda n: store.save_report(
            {"symbol": "000001.SZ", "content": str(n)}, documents=own,
        ), range(24)))
    assert len({row["id"] for row in rows}) == 24
    assert len(store.list_reports(documents=own)) == 20
    assert store.list_reports(documents=other) == []
    assert not store.delete_report(rows[-1]["id"], documents=other)
    assert store.delete_report(rows[-1]["id"], documents=own)
    assert store.clear_reports(documents=own) == 19


def test_original_report_routes_use_tenant_storage(env, monkeypatch):
    from app.api import financials, market_recap, stock_analysis

    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.state.capabilities = SimpleNamespace(has=lambda _: True)
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    for router in (financials.router, stock_analysis.router, market_recap.router):
        app.include_router(router)
    monkeypatch.setattr(JsonReportStore, "_path", lambda _: pytest.fail("global report file"))
    first, _ = register(env)
    second, _ = register(env)
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        for path, body in [
            ("/api/stock-analysis/reports", {"symbol": "000001.SZ", "content": "个股"}),
            ("/api/financials/reports", {"symbol": "000001.SZ", "content": "财务"}),
            ("/api/market-recap/reports", {"as_of": "2026-10-14", "content": "复盘"}),
        ]:
            client.cookies.set("tf_session", first)
            response = client.post(path, json=body)
            assert response.status_code == 200, response.text
            report_id = response.json()["report"]["id"]
            assert len(client.get(path).json()["reports"]) == 1
            client.cookies.set("tf_session", second)
            assert client.get(path).json()["reports"] == []
            assert client.delete(path + "/" + report_id).json() == {"ok": False}


async def test_assistant_private_watchlist_signals_and_platform_permission(env, monkeypatch, tmp_path):
    from app.services import watchlist

    _, first = register(env)
    _, second = register(env)
    own, other = documents(env, first), documents(env, second)
    monkeypatch.setattr(watchlist, "list_symbols", lambda: pytest.fail("global watchlist"))
    monkeypatch.setattr(watchlist, "add", lambda *a, **kw: pytest.fail("global watchlist write"))
    repo = SimpleNamespace(get_name_map=lambda symbols: {}, get_daily_asset=lambda *a: None)
    ctx = tools.ToolContext.build(repo=repo, data_dir=tmp_path, documents=own,
                                  account=first, verify_access=lambda: None)
    other_ctx = tools.ToolContext.build(repo=repo, data_dir=tmp_path, documents=other,
                                        account=second, verify_access=lambda: None)
    result = await tools.execute_assistant_tool("add_to_watchlist", {"symbol": "000001.SZ"}, ctx)
    assert result["ok"] and result["result"]["added"]
    result = await tools.execute_assistant_tool("add_to_watchlist", {"symbol": "000001.SZ"}, ctx)
    assert result["ok"] and not result["result"]["added"]
    assert (await tools.execute_assistant_tool("get_watchlist", {}, other_ctx))["result"]["rows"] == []
    result = await tools.execute_assistant_tool("create_signal_strategy", {
        "name": "价格", "conditions": [{"left": "close", "op": ">", "right": 10}],
    }, ctx)
    assert result["ok"], result
    assert (await tools.execute_assistant_tool("list_signals", {}, ctx))["result"]["count"] == 1
    assert (await tools.execute_assistant_tool("list_signals", {}, other_ctx))["result"]["count"] == 0
    assert "sync_data" not in {row["function"]["name"] for row in tools.build_tool_schemas(account=first)}
    assert not (await tools.execute_assistant_tool("sync_data", {}, ctx))["ok"]
    assert list(tmp_path.iterdir()) == []


async def test_confirmation_is_session_owned_durable_and_expires(env):
    from app.multitenancy.ai import SQLPendingRegistry

    token, first = register(env)
    own = documents(env, first)
    owner = SQLPendingRegistry(own, first, token, timeout_s=.03)
    restarted = SQLPendingRegistry(own, first, token, timeout_s=.03)
    different_session = SQLPendingRegistry(own, first, "another-session", timeout_s=.03)
    different_user = SQLPendingRegistry(own, {**first, "id": "another-user"}, token, timeout_s=.03)
    action = await owner.register(actions.new_call_id(), "add_to_watchlist", {"symbol": "000001.SZ"})
    assert await different_session.resolve(action.call_id, True) is None
    assert await different_user.resolve(action.call_id, True) is None
    assert await restarted.resolve(action.call_id, True) is not None
    assert await owner.await_decision(action) == "approved"
    assert await restarted.resolve(action.call_id, True) is None
    expired = await owner.register(actions.new_call_id(), "add_to_watchlist", {})
    await asyncio.sleep(.04)
    assert await restarted.resolve(expired.call_id, True) is None
    assert await owner.await_decision(expired) == "expired"


async def test_chat_rechecks_access_after_confirmation_and_cleans_pending(env, monkeypatch):
    from app.multitenancy.ai import SQLPendingRegistry

    token, first = register(env)
    own = documents(env, first)
    registry = SQLPendingRegistry(own, first, token)
    valid = True
    executed = []

    def verify():
        if not valid:
            raise AccessError(401, "账户权限已变更")

    async def model(*args, **kwargs):
        yield {"type": "round_end", "tool_calls": [
            {"id": "one", "name": "add_to_watchlist", "arguments": '{"symbol":"000001.SZ"}'},
        ]}

    async def execute(*args, **kwargs):
        executed.append(True)
        return {"ok": True, "result": {}}

    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", model)
    monkeypatch.setattr(tools, "execute_assistant_tool", execute)
    events = []
    async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "加入自选"}], documents=own, account=first,
        action_registry=registry, verify_access=verify,
    ):
        event = json.loads(line)
        events.append(event)
        if event["type"] == "action_confirm":
            valid = False
            await registry.resolve(event["call_id"], True)
    assert not executed
    assert any(event["type"] == "error" for event in events)
    assert all(row["value"]["decision"] != "pending" for row in own.records("assistant_actions"))


async def test_ai_iterator_saves_sql_draft_and_calls_durable_backtest(env, monkeypatch, tmp_path):
    from test_ai_iterate import V1_CODE, FakeGenerator

    from app.multitenancy import ai
    from app.multitenancy.strategies import TenantStrategyEngine, definition_payload
    from app.strategy import ai_iterator
    from app.strategy.ai_generator import AIStrategyGenerator
    from app.strategy.engine import StrategyEngine

    _, user = register(env)
    own = documents(env, user)
    builtin = StrategyEngine([Path(ai_iterator.__file__).parent / "builtin"])
    template = builtin.strategy_definitions()[0]
    calls = []

    class Executor:
        def execute(self, payload, *, cancel=None):
            assert cancel is None or not cancel.is_set()
            assert payload["kind"] == "describe"
            target = next(row for row in payload["sources"] if row["id"] == payload["strategy_id"])
            meta = AIStrategyGenerator._extract_meta(target["code"])
            return {"definition": definition_payload(replace(
                template, meta=meta, source="ai", required_features=frozenset(),
            ))}

    engine = TenantStrategyEngine(own, builtin, Executor())

    async def backtest(engine, **kwargs):
        calls.append((engine.documents.context.tenant_id, kwargs))
        return {"stats": {"sharpe": 1.5}, "strategy_id": kwargs["strategy_id"]}

    async def model(messages, schemas, *, execute_tool, **kwargs):
        draft = next(row["id"] for row in own.records("strategy_sources"))
        result = await execute_tool("run_backtest", {"strategy_id": draft})
        return [{"role": "tool", "content": json.dumps(result)},
                {"role": "assistant", "content": "```python\n" + V1_CODE + "\n```"}]

    monkeypatch.setattr(ai, "run_tool_backtest", backtest)
    monkeypatch.setattr(ai_iterator, "AIStrategyGenerator", FakeGenerator)
    monkeypatch.setattr(ai_iterator, "generate_ai_text_with_tools", model)
    monkeypatch.setattr(StrategyEngine, "_load_file", lambda *a: pytest.fail("private import in API"))
    result = await ai_iterator.AIStrategyIterator(max_rounds=1).iterate(
        "生成策略", engine=engine, data_dir=own,
    )
    draft = result["draft_strategy_id"]
    assert own.get("strategy_sources", draft)["definition"]["meta"]["research_only"]
    assert not any(row["id"] == draft for row in engine.list_strategies())
    assert calls and calls[0][0] == user["default_tenant_id"]
    assert list(tmp_path.iterdir()) == []


async def test_ai_backtest_runs_full_durable_job_and_cancellation_is_persisted(env):
    from test_full_research import Compute

    from app.multitenancy.ai import run_tool_backtest
    from app.multitenancy.research import ResearchJobs, ResearchWorker
    from app.multitenancy.strategies import TenantStrategyEngine
    from app.strategy.engine import StrategyEngine

    _, user = register(env)
    own = documents(env, user)
    builtin = StrategyEngine([Path(__file__).parents[1] / "app" / "strategy" / "builtin"])
    compute = Compute({"stats": {"sharpe": 1.75, "total_return": .1, "private_extra": "hidden"}})
    engine = TenantStrategyEngine(own, builtin, compute)
    strategy_id = builtin.strategy_definitions()[0].meta["id"]
    jobs = ResearchJobs(own)
    worker = ResearchWorker(env["db"], compute)
    try:
        task = asyncio.create_task(run_tool_backtest(engine, strategy_id=strategy_id))
        claim = None
        for _ in range(100):
            if task.done():
                await task
            claim = await asyncio.to_thread(worker.claim, own.context)
            if claim:
                break
            await asyncio.sleep(.01)
        assert claim is not None
        await asyncio.to_thread(worker.run, own.context, claim)
        result = await task
        assert result["stats"] == {"sharpe": 1.75, "total_return": .1}
        assert jobs.get(claim["id"])["metadata"]["source"] == "ai"
        assert compute.calls[0]["kind"] == "backtest"
        assert compute.calls[0]["tenant_id"] == user["default_tenant_id"]
        second = asyncio.create_task(run_tool_backtest(engine, strategy_id=strategy_id))
        pending = None
        for _ in range(100):
            with env["db"].business_transaction(own.context) as conn:
                pending = conn.execute(
                    "SELECT * FROM business.research_jobs WHERE state='queued'"
                ).fetchone()
            if pending:
                break
            await asyncio.sleep(.01)
        assert pending is not None
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert jobs.get(pending["id"])["state"] == "cancelled"
    finally:
        worker.close()


async def test_ai_stream_idle_revocation_closes_upstream(env, monkeypatch):
    from app.multitenancy import ai
    from app.services.ndjson_heartbeat import with_heartbeat

    token, user = register(env)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(account_identity=env["identity"])),
        state=SimpleNamespace(account=user, tenant_context=documents(env, user).context, request_id="stream"),
        cookies={"tf_session": token},
    )
    stopped = asyncio.Event()

    async def producer():
        try:
            yield '{"type":"meta"}'
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(ai, "with_heartbeat", lambda events: with_heartbeat(events, interval=.01))
    stream = ai.authorized_lines(request, producer())
    assert json.loads(await anext(stream))["type"] == "meta"
    env["identity"].logout(token)
    assert json.loads(await anext(stream))["type"] == "error"
    assert json.loads(await anext(stream))["type"] == "done"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert stopped.is_set()


async def test_cancel_during_sql_submission_does_not_leave_orphan_ai_job(env, monkeypatch):
    import threading

    from app.multitenancy.ai import run_tool_backtest
    from app.multitenancy.research import ResearchJobs
    from app.multitenancy.strategies import TenantStrategyEngine
    from app.strategy.engine import StrategyEngine

    _, user = register(env)
    own = documents(env, user)
    builtin = StrategyEngine([Path(__file__).parents[1] / "app" / "strategy" / "builtin"])
    engine = TenantStrategyEngine(own, builtin, None)
    started, release = threading.Event(), threading.Event()
    original = ResearchJobs.submit
    saved = []

    def submit(self, *args, **kwargs):
        started.set()
        assert release.wait(5)
        row = original(self, *args, **kwargs)
        saved.append(row)
        return row

    monkeypatch.setattr(ResearchJobs, "submit", submit)
    task = asyncio.create_task(run_tool_backtest(engine, strategy_id=builtin.strategy_definitions()[0].meta["id"]))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved and ResearchJobs(own).get(saved[0]["id"])["state"] == "cancelled"


async def test_disconnected_iteration_cancels_description_and_does_not_save_draft(env, monkeypatch):
    import threading

    from test_ai_iterate import FakeGenerator

    from app.multitenancy.ai import connected_result
    from app.multitenancy.strategies import TenantStrategyEngine
    from app.strategy import ai_iterator
    from app.strategy.engine import StrategyEngine

    _, user = register(env)
    own = documents(env, user)
    stopped = threading.Event()

    class Executor:
        def execute(self, payload, *, cancel):
            assert payload["kind"] == "describe"
            try:
                assert cancel.wait(3)
                raise AccessError(409, "计算已取消")
            finally:
                stopped.set()

    async def disconnected():
        return True

    builtin = StrategyEngine([Path(__file__).parents[1] / "app" / "strategy" / "builtin"])
    engine = TenantStrategyEngine(own, builtin, Executor())
    monkeypatch.setattr(ai_iterator, "AIStrategyGenerator", FakeGenerator)
    iterator = ai_iterator.AIStrategyIterator(max_rounds=1)
    with pytest.raises(AccessError) as error:
        await connected_result(SimpleNamespace(is_disconnected=disconnected),
                               iterator.iterate("草稿", engine=engine, data_dir=own))
    assert error.value.status == 499
    assert await asyncio.to_thread(stopped.wait, 3)
    assert own.records("strategy_sources") == []


async def test_tenant_ai_configuration_hint_points_to_admin(env, monkeypatch):
    _, user = register(env)
    monkeypatch.setattr(chat_service, "ai_configured", lambda: False)
    events = [json.loads(line) async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "分析"}], account=user,
    )]
    assert "管理员" in events[0]["message"]
    assert "hint" not in events[0]
