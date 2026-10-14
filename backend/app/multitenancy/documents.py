"""Explicit tenant storage for the full product's structured private state.

This handle is deliberately not a filesystem path. Public repositories keep
their original data directory; passing this handle to an unconverted file
operation fails instead of silently using another tenant's files.
"""
# ruff: noqa: RUF001 -- Chinese business messages use localized punctuation.
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from fastapi import Request

from app.cell.context import AccessError, TenantContext

if TYPE_CHECKING:
    from app.cell.database import Databases

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_NAMESPACE_ITEMS = 2000


def json_value(value: Any) -> Any:
    def encode(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"unsupported document value: {type(item).__name__}")

    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, default=encode)
    except (TypeError, ValueError) as exc:
        raise AccessError(400, "业务记录格式错误") from exc
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise AccessError(413, "业务记录过大")
    return json.loads(text)


@dataclass(frozen=True, slots=True)
class TenantDocuments:
    db: Databases
    context: TenantContext

    @contextmanager
    def _transaction(self, *, write=False):
        if isinstance(self, DocumentSession):
            if write and not self.writable:
                raise RuntimeError("document mutation attempted in a read transaction")
            yield self.connection
            return
        with self.db.business_transaction(self.context, write=write) as conn:
            yield conn

    @contextmanager
    def transaction(self, *, write=True):
        """Explicit unit of work for related domain records."""
        with self._transaction(write=write) as conn:
            yield DocumentSession(self.db, self.context, conn, write)

    @staticmethod
    def _key(namespace: str, item_id: str) -> None:
        if not namespace or len(namespace) > 80 or not item_id or len(item_id) > 256:
            raise ValueError("invalid private document key")

    def record(self, namespace: str, item_id: str) -> dict | None:
        self._key(namespace, item_id)
        with self._transaction() as conn:
            return conn.execute(
                """SELECT id,value,revision,updated_at FROM business.documents
                   WHERE namespace=%s AND id=%s""", (namespace, item_id),
            ).fetchone()

    def get(self, namespace: str, item_id: str, default=None):
        row = self.record(namespace, item_id)
        return copy.deepcopy(row["value"] if row else default)

    def records(self, namespace: str) -> list[dict]:
        self._key(namespace, "list")
        with self._transaction() as conn:
            return conn.execute(
                """SELECT id,value,revision,updated_at FROM business.documents
                   WHERE namespace=%s ORDER BY id""", (namespace,),
            ).fetchall()

    def values(self, namespace: str) -> list:
        return [row["value"] for row in self.records(namespace)]

    def mutate(
        self, namespace: str, item_id: str, change: Callable[[Any], Any],
        *, expected_revision: int | None = None,
        expected_collection: dict[str, int] | None = None,
    ) -> Any:
        """One atomic read/modify/write, serialized by the tenant binding lock."""
        from psycopg.types.json import Jsonb

        self._key(namespace, item_id)
        with self._transaction(write=True) as conn:
            self._compare_collection(conn, namespace, expected_collection)
            row = conn.execute(
                "SELECT value,revision FROM business.documents WHERE namespace=%s AND id=%s",
                (namespace, item_id),
            ).fetchone()
            if expected_revision is not None and expected_revision != (row["revision"] if row else 0):
                raise AccessError(409, "记录已更新，请刷新后重试")
            value = json_value(change(copy.deepcopy(row["value"]) if row else None))
            if row is None:
                count = conn.execute(
                    "SELECT count(*) AS n FROM business.documents WHERE namespace=%s", (namespace,),
                ).fetchone()["n"]
                if count >= MAX_NAMESPACE_ITEMS:
                    raise AccessError(429, "业务记录数量已达上限")
            conn.execute(
                """INSERT INTO business.documents(tenant_id,namespace,id,value)
                   VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET
                     value=excluded.value,revision=business.documents.revision+1,updated_at=now()""",
                (self.context.tenant_id, namespace, item_id, Jsonb(value)),
            )
        return value

    @staticmethod
    def _compare_collection(conn, namespace: str, expected: dict[str, int] | None):
        if expected is not None:
            actual = conn.execute(
                "SELECT id,revision FROM business.documents WHERE namespace=%s", (namespace,),
            ).fetchall()
            if {row["id"]: row["revision"] for row in actual} != expected:
                raise AccessError(409, "关联记录已更新，请刷新后重试")

    def put(
        self, namespace: str, item_id: str, value, *, expected_revision: int | None = None,
        expected_collection: dict[str, int] | None = None,
    ):
        return self.mutate(namespace, item_id, lambda _: value, expected_revision=expected_revision,
                           expected_collection=expected_collection)

    def delete(
        self, namespace: str, item_id: str, *, expected_collection: dict[str, int] | None = None,
    ) -> bool:
        self._key(namespace, item_id)
        with self._transaction(write=True) as conn:
            self._compare_collection(conn, namespace, expected_collection)
            return conn.execute(
                "DELETE FROM business.documents WHERE namespace=%s AND id=%s",
                (namespace, item_id),
            ).rowcount > 0


@dataclass(frozen=True, slots=True)
class DocumentSession(TenantDocuments):
    connection: Any
    writable: bool


def private_storage(request: Request) -> TenantDocuments | None:
    identity = getattr(getattr(getattr(request, "app", None), "state", None), "account_identity", None)
    if identity is None:
        return None
    context = getattr(request.state, "tenant_context", None)
    if context is None:
        raise AccessError(409, "请先选择业务租户")
    return TenantDocuments(identity.db, context)
