"""Stateless Responses adapter shared by reports, strategy tools and assistant.

Only a completed response authorizes tool execution. Native output (including
encrypted reasoning) is replayed within the current request, never by a global
previous_response_id or a provider-side stored conversation.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

from app.services import ai_provider

OUTPUT_KEY = "_responses_output"


def chat_input(messages: Sequence[dict]) -> list[dict]:
    items: list[dict] = []
    for message in messages:
        role = message.get("role", "user")
        if role == "tool":
            items.append({
                "type": "function_call_output", "call_id": message["tool_call_id"],
                "output": str(message.get("content") or ""),
            })
        elif role == "assistant" and OUTPUT_KEY in message:
            items.extend(message[OUTPUT_KEY])
        elif role == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                items.append({"role": role, "content": message["content"]})
            for call in message["tool_calls"]:
                items.append({
                    "type": "function_call", "call_id": call["id"],
                    "name": call["function"]["name"], "arguments": call["function"]["arguments"],
                })
        else:
            items.append({"role": role, "content": str(message.get("content") or "")})
    return items


def _payload(messages, tools, temperature, max_tokens) -> dict:
    payload: dict[str, Any] = {
        "model": ai_provider.current_ai_model(), "input": chat_input(messages),
        "store": False, "include": ["reasoning.encrypted_content"],
    }
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if ai_provider.current_ai_provider() == ai_provider.OPENAI_PROVIDER:
        effort = ai_provider.current_openai_reasoning_effort()
        if effort:
            payload["reasoning"] = {"effort": effort}
    if tools:
        flat = []
        for tool in tools:
            fn = tool.get("function", {})
            if tool.get("type") != "function" or not fn.get("name"):
                raise ValueError("Responses 仅支持已声明的 function 工具")
            # Responses defaults to strict; existing optional Chat parameters
            # must remain optional unless their schema explicitly opts in.
            flat.append({**fn, "type": "function", "strict": fn.get("strict", False)})
        payload["tools"] = flat
    return payload


def _retry_kwargs(exc, payload) -> dict | None:
    retry = dict(payload)
    if "temperature" in retry and ai_provider._is_temperature_rejected(exc):
        retry.pop("temperature")
        return retry
    if "reasoning" in retry and getattr(exc, "status_code", None) == 400:
        param = ai_provider._openai_error_param(exc)
        detail = ai_provider._openai_error_detail(exc).lower()
        if param in {"reasoning", "reasoning.effort"} or "reasoning" in detail:
            retry.pop("reasoning")
            return retry
    return None


async def _create(client, payload, *, stream):
    while True:
        try:
            return await client.responses.create(**payload, stream=stream)
        except Exception as exc:
            retry = _retry_kwargs(exc, payload)
            if retry is not None:
                payload = retry
                continue
            if ai_provider._is_openai_transport_error(exc):
                raise RuntimeError(ai_provider._format_openai_error(exc)) from exc
            raise


def _client(timeout):
    key = ai_provider.secrets_store.get_ai_key()
    if not key:
        raise RuntimeError("AI API Key 未配置, 请在设置页配置")
    return ai_provider._openai_client(key, timeout)


def _result(response, tools):
    status = getattr(response, "status", None)
    if status == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "")
        if reason == "max_output_tokens":
            raise RuntimeError("AI 输出达到长度上限, 内容不完整; 请提高输出 Token 上限后重试")
        raise RuntimeError("AI 响应不完整, 本轮工具未执行")
    if status == "failed":
        error = getattr(getattr(response, "error", None), "message", "")
        raise RuntimeError(f"AI 服务请求失败: {ai_provider._compact_error_text(error or '上游未返回详情')}")
    if status != "completed":
        raise RuntimeError("AI 响应缺少 completed 终态, 本轮工具未执行")

    output = [item.model_dump(exclude_none=True) for item in (response.output or [])]
    allowed = {tool["function"]["name"] for tool in (tools or [])}
    calls, text, seen = [], [], set()
    for item in output:
        kind = item.get("type")
        if kind == "message":
            if item.get("status") != "completed":
                raise RuntimeError("AI 消息不完整")
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise RuntimeError("AI 拒绝生成本次内容")
                if part.get("type") != "output_text":
                    raise RuntimeError("AI 返回了不支持的正文类型")
                text.append(part.get("text", ""))
        elif kind == "function_call":
            call_id, name, raw = item.get("call_id"), item.get("name"), item.get("arguments", "")
            if not call_id or call_id in seen or name not in allowed or item.get("status") != "completed":
                raise RuntimeError("AI 工具调用不完整、重复或未声明, 本轮工具未执行")
            try:
                args = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("AI 工具参数不是完整 JSON, 本轮工具未执行") from exc
            if not isinstance(args, dict):
                raise RuntimeError("AI 工具参数须为 JSON 对象, 本轮工具未执行")
            seen.add(call_id)
            calls.append(SimpleNamespace(
                id=call_id, type="function",
                function=SimpleNamespace(name=name, arguments=raw),
            ))
        elif kind != "reasoning":
            raise RuntimeError("AI 返回了不支持的输出类型, 本轮工具未执行")
    content = "".join(text)
    if not content.strip() and not calls:
        raise RuntimeError("AI 服务未返回正文内容; 请检查模型配置或稍后重试")
    return SimpleNamespace(content=content, tool_calls=calls, responses_output=output)


async def run_message_once(
    messages, *, temperature, max_tokens, timeout, tools=None,
):
    payload = _payload(messages, tools, temperature, max_tokens)
    client = _client(timeout)
    try:
        response = await _create(client, payload, stream=False)
        return _result(response, tools)
    finally:
        await client.close()


async def stream_round(
    messages, tools=(), *, temperature=0.3, max_tokens=None, timeout=240.0,
) -> AsyncIterator[dict[str, Any]]:
    payload = _payload(messages, tools, temperature, max_tokens)
    client = _client(timeout)
    stream = None
    try:
        stream = await _create(client, payload, stream=True)
        emitted: list[str] = []
        completed = False
        async for event in stream:
            kind = event.type
            if kind == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    emitted.append(delta)
                    yield {"type": "text", "delta": delta}
            elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                result = _result(event.response, tools)
                emitted_text = "".join(emitted)
                if not result.content.startswith(emitted_text):
                    raise RuntimeError("AI 流式正文与最终响应不一致, 本轮工具未执行")
                remainder = result.content[len(emitted_text):]
                if remainder:
                    yield {"type": "text", "delta": remainder}
                completed = True
                yield {
                    "type": "round_end",
                    "tool_calls": [
                        {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
                        for call in result.tool_calls
                    ],
                    "finish_reason": "tool_calls" if result.tool_calls else "stop",
                    OUTPUT_KEY: result.responses_output,
                }
                break
            elif kind == "error":
                detail = ai_provider._compact_error_text(getattr(event, "message", "") or "上游流错误")
                raise RuntimeError(f"AI 服务请求失败: {detail}")
            # Function deltas/items are deliberately not executable. Only the
            # complete response.output above supplies the authorized tool list.
        if not completed:
            raise RuntimeError("AI 流缺少 completed 终态, 内容不完整且本轮工具未执行")
    except Exception as exc:
        if ai_provider._is_openai_transport_error(exc):
            raise RuntimeError(ai_provider._format_openai_error(exc)) from exc
        raise
    finally:
        try:
            if stream is not None:
                await stream.close()
        finally:
            await client.close()
