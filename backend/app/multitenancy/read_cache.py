"""Coalesce identical public K-line reads at the single platform process."""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict

from starlette.responses import Response

MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 128


class PublicReadCache:
    def __init__(self):
        self.values = OrderedDict()
        self.locks = {}
        self.bytes = 0

    async def read(self, request, call_next):
        body = await request.body()
        key = hashlib.sha256(
            (request.method + request.url.path + request.url.query
             + request.headers.get("accept-encoding", "")).encode() + body,
        ).hexdigest()
        lock, users = self.locks.setdefault(key, [asyncio.Lock(), 0])
        self.locks[key][1] = users + 1
        try:
            async with lock:
                cached = self.values.get(key)
                if cached and time.monotonic() - cached[0] < 2:
                    self.values.move_to_end(key)
                    return Response(cached[1], headers=cached[2])
                response = await call_next(request)
                if response.status_code != 200 or "no-store" in response.headers.get("cache-control", "").lower():
                    return response
                content = b"".join([chunk async for chunk in response.body_iterator])
                headers = {name: value for name, value in response.headers.items()
                           if name in {"content-type", "content-encoding", "content-disposition"}}
                if len(content) <= MAX_BYTES:
                    if key in self.values:
                        self.bytes -= len(self.values.pop(key)[1])
                    while self.values and (self.bytes + len(content) > MAX_BYTES
                                           or len(self.values) >= MAX_ENTRIES):
                        _, old = self.values.popitem(last=False)
                        self.bytes -= len(old[1])
                    self.values[key] = (time.monotonic(), content, headers)
                    self.bytes += len(content)
                return Response(content, headers=headers)
        finally:
            self.locks[key][1] -= 1
            if not self.locks[key][1]:
                del self.locks[key]


_cache = PublicReadCache()
cached_read = _cache.read
