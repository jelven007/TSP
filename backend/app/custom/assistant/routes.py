"""AI 助手 HTTP 端点 — 薄层: 参数校验 + 流式响应映射。

POST /api/custom/assistant/chat      NDJSON 事件流(协议见模块 __init__)
GET  /api/custom/assistant/status     供应商配置状态(前端门控展示)
GET  /api/custom/assistant/suggests   空会话快捷指令
POST /api/custom/assistant/actions/{call_id}/decision
                                     动作工具确认/拒绝(确认卡按钮回调)
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import COOKIE_NAME
from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import tools as assistant_tools
from app.custom.assistant.chat_service import chat_stream
from app.custom.assistant.sessions import SessionCollection, SessionStore
from app.multitenancy.access import verify_live_access
from app.multitenancy.ai import SQLPendingRegistry, authorized_lines
from app.multitenancy.documents import private_storage
from app.services.ai_provider import (
    ai_configured,
    current_ai_model,
    current_ai_provider,
    is_codex_cli_provider,
)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class AssistantContext(BaseModel):
    page: str = Field(default="", max_length=100)
    symbol: str = Field(default="", max_length=32)


class ChatRequest(BaseModel):
    messages: list[HistoryMessage] = Field(min_length=1, max_length=200)
    context: AssistantContext | None = None


class ActionDecisionRequest(BaseModel):
    approve: bool


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/custom/assistant", tags=["custom-assistant"])

    def session_store(request: Request) -> SessionStore:
        documents = private_storage(request)
        if documents is not None:
            return SessionStore(documents=documents)
        from app.services import auth

        if request.headers.get("authorization"):
            raise HTTPException(status_code=403, detail="会话同步需要账户登录")
        repo = getattr(request.app.state, "repo", None)
        user = auth.current_user(request.cookies.get(COOKIE_NAME, ""))
        return SessionStore(
            data_dir=repo.store.data_dir if repo is not None else None,
            owner=str(user["id"]) if user else "standalone",
        )

    @router.get("/sessions")
    def read_sessions(request: Request):
        from fastapi.responses import JSONResponse

        return JSONResponse(session_store(request).read(), headers={"Cache-Control": "no-store"})

    @router.put("/sessions")
    def save_sessions(request: Request, req: SessionCollection):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            session_store(request).write(
                [row.model_dump(exclude_unset=True) for row in req.sessions], req.revision,
            ),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/chat")
    async def chat(request: Request, req: ChatRequest):
        engine = getattr(request.app.state, "strategy_engine", None)
        repo = getattr(request.app.state, "repo", None)
        data_dir = str(repo.store.data_dir) if repo is not None else None
        context = req.context.model_dump() if req.context else None
        documents = private_storage(request)
        options = {}
        if documents is not None:
            from app.api.strategy import _get_engine

            engine = _get_engine(request)
            account = request.state.account
            options = {"documents": documents, "account": account,
                       "verify_access": lambda: verify_live_access(request),
                       "action_registry": SQLPendingRegistry(
                           documents, account, request.cookies.get(COOKIE_NAME, ""),
                       )}

        async def gen():
            async for line in authorized_lines(request, chat_stream(
                history=[m.model_dump() for m in req.messages],
                context=context,
                engine=engine,
                data_dir=data_dir,
                repo=repo,
                quote_service=getattr(request.app.state, "quote_service", None),
                depth_service=getattr(request.app.state, "depth_service", None),
                capabilities=getattr(request.app.state, "capabilities", None),
                financial_scheduler=getattr(request.app.state, "financial_scheduler", None),
                **options,
            )):
                yield line

        return StreamingResponse(
            gen(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/status")
    def status() -> dict:
        configured = ai_configured()
        return {
            "configured": configured,
            "provider": current_ai_provider(),
            "model": current_ai_model(),
            "supports_tools": configured and not is_codex_cli_provider(),
        }

    @router.get("/suggests")
    def suggests(request: Request) -> dict:
        items = assistant_tools.QUICK_SUGGESTS
        account = getattr(request.state, "account", None)
        if account is not None and "data:manage" not in account["permissions"]:
            items = [row for row in items if row["group"] != "数据与扩展"]
        return {"suggests": items}

    @router.post("/actions/{call_id}/decision")
    async def action_decision(request: Request, call_id: str, req: ActionDecisionRequest) -> dict:
        """确认卡回调: 唤醒挂起中的动作工具(批准执行或拒绝)。"""
        documents = private_storage(request)
        registry = assistant_actions.registry
        if documents is not None:
            registry = SQLPendingRegistry(documents, request.state.account, request.cookies.get(COOKIE_NAME, ""))
        action = await registry.resolve(call_id, req.approve)
        if action is None:
            raise HTTPException(status_code=404, detail="确认请求不存在或已过期")
        return {"ok": True, "call_id": call_id, "decision": "approved" if req.approve else "denied"}

    return router
