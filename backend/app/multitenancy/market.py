"""Public market snapshot transport. No tenant state or provider calls."""
from __future__ import annotations

import base64
import io
import json
from datetime import date

import httpx
import polars as pl
import pyarrow.ipc as ipc

from app.cell.context import AccessError

MAX_FRAME_BYTES = 64 * 1024 * 1024


def settlement_days(data_dir):
    from app.market_time import cn_today, published_trading_days

    today = cn_today()
    try:
        marker = json.loads((data_dir / "settlement_ready.json").read_text())
        finalized = date.fromisoformat(marker["day"])
    except (OSError, ValueError, KeyError):
        finalized = None
    days = []
    for partition in (data_dir / "kline_daily").glob("date=*"):
        try:
            day = date.fromisoformat(partition.name.removeprefix("date="))
        except ValueError:
            continue
        if day > today or (day == today and finalized != day) or not any(partition.glob("*.parquet")):
            continue
        official = published_trading_days(day.year)
        if official is not None and day not in official:
            continue
        if official is None and not any((data_dir / "kline_index_daily" / partition.name).glob("*.parquet")):
            continue
        days.append(day.isoformat())
    return sorted(days)


def encode_frame(frame):
    data = frame.write_ipc(None, compression="uncompressed").getvalue()
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("public snapshot exceeds limit")
    return base64.b64encode(data).decode("ascii")


def decode_frame(encoded):
    if not isinstance(encoded, str) or len(encoded) > MAX_FRAME_BYTES * 4 // 3 + 4:
        raise ValueError("invalid public snapshot")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("public snapshot exceeds limit")
    reader = ipc.open_file(io.BytesIO(data))
    if len(reader.schema) > 512 or sum(reader.get_batch(i).num_rows for i in range(reader.num_record_batches)) > 100000:
        raise ValueError("public snapshot exceeds row/column limit")
    return pl.read_ipc(io.BytesIO(data), memory_map=False)


class PublicMarket:
    def __init__(self, config):
        if not config.market_url or not config.market_token.get_secret_value():
            raise RuntimeError("full-product service requires a public market gateway")
        self.client = httpx.Client(
            base_url=config.market_url.rstrip("/"),
            headers={"Authorization": "Bearer " + config.market_token.get_secret_value()},
            timeout=15, follow_redirects=False, trust_env=False,
        )

    def snapshot(self):
        try:
            response = self.client.get("/internal/cell-market/snapshot")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AccessError(503, "共享行情暂不可用") from exc

    def close(self):
        self.client.close()
