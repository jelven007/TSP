"""Tenant-owned original files retained during the full-product migration."""
# ruff: noqa: RUF001 -- localized business messages.
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.cell.context import AccessError
from app.multitenancy.documents import private_storage

router = APIRouter(prefix="/api/backtest/legacy", tags=["research"])


@router.get("")
def list_originals(request: Request):
    documents = private_storage(request)
    with documents._transaction() as conn:
        rows = conn.execute(
            """SELECT path,sha256,octet_length(content) AS bytes
               FROM business.legacy_artifacts ORDER BY path"""
        ).fetchall()
    return {"items": rows}


@router.get("/download")
def download_original(request: Request, path: str = Query(max_length=1024),
                      sha256: str = Query(pattern="^[a-f0-9]{64}$")):
    documents = private_storage(request)
    with documents._transaction() as conn:
        row = conn.execute(
            "SELECT content,sha256 FROM business.legacy_artifacts WHERE path=%s", (path,),
        ).fetchone()
    if not row:
        raise AccessError(404, "历史文件不存在")
    if row["sha256"] != sha256:
        raise AccessError(409, "历史文件已更新，请刷新列表")
    return Response(bytes(row["content"]), media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.rsplit('/', 1)[-1], safe='')}",
        "X-Content-Type-Options": "nosniff",
    })
