"""Bounded account-owned history with collection revision (including deletes)."""
# ruff: noqa: RUF001 -- localized messages.
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments

MAX_BYTES = 4 * 1024 * 1024
MAX_SESSIONS = 20
MAX_MESSAGES = 200
Key = Annotated[str, Field(min_length=1, max_length=128)]
Text = Annotated[str, Field(max_length=200000)]
Time = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Chart(BaseModel):
    kind: Literal["daily_kline", "intraday"]
    symbol: Key
    name: str = ""
    prev_close: float | None = Field(default=None, allow_inf_nan=False)
    points: list[list[str | Annotated[float, Field(allow_inf_nan=False)]]] = Field(max_length=240)

    @model_validator(mode="after")
    def point_shape(self):
        size = 6 if self.kind == "daily_kline" else 3
        if any(len(row) != size or not isinstance(row[0], str)
               or any(not isinstance(value, (float, int)) for value in row[1:]) for row in self.points):
            raise ValueError("invalid chart point")
        return self


class Confirmation(BaseModel):
    label: Text
    risk: Text


class ToolCall(BaseModel):
    callId: Key  # noqa: N815 -- existing browser history format.
    name: Key
    args: dict[str, JsonValue]
    status: Literal["running", "awaiting", "approved", "denied", "ok", "error"]
    summary: Text | None = None
    elapsedMs: Time | None = None  # noqa: N815 -- existing browser history format.
    charts: list[Chart] | None = Field(default=None, max_length=20)
    confirm: Confirmation | None = None


class Message(BaseModel):
    id: Key
    role: Literal["user", "assistant", "footprint", "error", "notice"]
    ts: Time
    content: Text | None = None
    streaming: bool = False
    calls: list[ToolCall] | None = Field(default=None, max_length=500)
    kind: Key | None = None
    message: Text | None = None
    hint: Text | None = None

    @model_validator(mode="after")
    def role_fields(self):
        if self.role in {"user", "assistant", "notice"} and self.content is None:
            raise ValueError("missing message content")
        if self.role == "footprint" and self.calls is None:
            raise ValueError("missing tool calls")
        if self.role == "error" and (self.kind is None or self.message is None):
            raise ValueError("missing error message")
        # Stored history never represents a live generation/approval request.
        self.streaming = False
        for call in self.calls or []:
            if call.status in {"running", "awaiting", "approved"}:
                call.status = "denied"
        return self


class Session(BaseModel):
    id: Key
    title: Annotated[str, Field(max_length=200)]
    createdAt: Time  # noqa: N815 -- existing browser history format.
    messages: list[Message] = Field(max_length=MAX_MESSAGES)


class SessionCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    sessions: list[Session] = Field(max_length=MAX_SESSIONS)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({row.id for row in self.sessions}) != len(self.sessions):
            raise ValueError("duplicate session id")
        return self


class SessionStore:
    def __init__(
        self, *, documents: TenantDocuments | None = None,
        data_dir: Path | None = None, owner: str | None = None,
    ):
        self.documents = documents
        # Never accept an owner from the request body.
        self.owner = documents.context.user_id if documents is not None else owner
        if not self.owner or (documents is None and data_dir is None):
            raise AccessError(503, "会话存储尚未就绪")
        self.data_dir = data_dir

    def _local(self):
        # Standalone storage also supports multiple legacy login accounts. SQLite
        # provides inter-process CAS on all supported platforms without a new dependency.
        path = Path(self.data_dir) / "user_data" / "assistant_sessions.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions "
                "(owner TEXT PRIMARY KEY, revision INTEGER NOT NULL, value TEXT NOT NULL)"
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def read(self) -> dict:
        if self.documents is not None:
            row = self.documents.record("assistant_sessions", self.owner)
            return {"revision": row["revision"], "sessions": row["value"]} if row else {
                "revision": 0, "sessions": [],
            }
        with closing(self._local()) as connection:
            row = connection.execute(
                "SELECT revision,value FROM sessions WHERE owner=?", (self.owner,),
            ).fetchone()
        return {"revision": row[0], "sessions": json.loads(row[1])} if row else {
            "revision": 0, "sessions": [],
        }

    def write(self, sessions: list[dict], revision: int) -> dict:
        text = json.dumps(sessions, ensure_ascii=False, allow_nan=False)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise AccessError(413, "会话历史超过 4 MiB，请删除部分会话后重试")
        if self.documents is not None:
            with self.documents.transaction() as documents:
                documents.put("assistant_sessions", self.owner, sessions, expected_revision=revision)
                row = documents.record("assistant_sessions", self.owner)
                return {"revision": row["revision"], "sessions": row["value"]}
        with closing(self._local()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM sessions WHERE owner=?", (self.owner,),
            ).fetchone()
            if revision != (row[0] if row else 0):
                raise AccessError(409, "会话已在其他设备更新，请处理同步冲突")
            connection.execute(
                "INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                "revision=excluded.revision,value=excluded.value", (self.owner, revision + 1, text),
            )
        return {"revision": revision + 1, "sessions": sessions}
