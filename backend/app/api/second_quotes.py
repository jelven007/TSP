"""Bounded inspection and validation API for second-level quote replay data."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.backtest.second_execution import (
    ExecutionConfig,
    OpeningLot,
    SecondOrder,
    validate_execution,
)
from app.backtest.second_replay import SecondQuoteReplay, SecondReplayDataError

router = APIRouter(prefix="/api/backtest/second-quotes", tags=["backtest"])

_MAX_VALIDATE_DAYS = 31
_MAX_SAMPLE_DAYS = 31


class SecondQuoteRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self):
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        return self


class SecondQuoteValidateRequest(SecondQuoteRange):
    expected_interval_s: float = Field(default=1.0, gt=0, le=60)
    expected_symbols: int | None = Field(default=None, gt=0)
    min_coverage_ratio: float = Field(default=1.0, gt=0, le=1)
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=200)
    asset_type: Literal["stock", "etf", "index"] = "stock"
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    trading_dates: list[date] | None = Field(default=None, min_length=1, max_length=31)
    max_source_age_ms: int = Field(default=5000, ge=0, le=60_000)


class SecondQuoteSampleRequest(SecondQuoteRange):
    symbols: list[str] = Field(min_length=1, max_length=200)
    limit: int = Field(default=1_000, ge=1, le=100_000)


class SecondExecutionRequest(BaseModel):
    day: date
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    orders: list[SecondOrder] = Field(min_length=1, max_length=1000)
    opening_lots: list[OpeningLot] = Field(default_factory=list, max_length=1000)
    config: ExecutionConfig = Field(default_factory=ExecutionConfig)


@router.post("/execution-validate")
def execution_validate(payload: SecondExecutionRequest, request: Request) -> dict:
    _replay(request)  # Use the same repository availability contract.
    if len({order.symbol for order in payload.orders}) > 200:
        raise HTTPException(status_code=422, detail="单次执行验证最多 200 只股票")
    try:
        return validate_execution(
            request.app.state.repo.store.data_dir,
            payload.day, start_ms=payload.start_ms, end_ms=payload.end_ms,
            orders=payload.orders, opening_lots=payload.opening_lots, config=payload.config,
        )
    except SecondReplayDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _replay(request: Request) -> SecondQuoteReplay:
    repo = getattr(request.app.state, "repo", None)
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if data_dir is None:
        raise HTTPException(status_code=503, detail="本地数据仓库不可用")
    return SecondQuoteReplay(data_dir)


def _guard_range(start: date, end: date, max_days: int) -> None:
    if (end - start).days + 1 > max_days:
        raise HTTPException(
            status_code=400,
            detail=f"秒级快照单次最多读取 {max_days} 个自然日",
        )


@router.get("/status")
def second_quote_status(request: Request) -> dict:
    replay = _replay(request)
    dates = replay.available_dates()
    quote_service = getattr(request.app.state, "quote_service", None)
    archive = None
    if quote_service is not None:
        archive = quote_service.status().get("second_quote_archive")
    return {
        "archive": archive,
        "available_dates": [value.isoformat() for value in dates],
        "earliest_date": dates[0].isoformat() if dates else None,
        "latest_date": dates[-1].isoformat() if dates else None,
    }


@router.post("/validate")
def validate_second_quotes(
    payload: SecondQuoteValidateRequest,
    request: Request,
) -> dict:
    _guard_range(payload.start, payload.end, _MAX_VALIDATE_DAYS)
    try:
        return _replay(request).validate(
            payload.start,
            payload.end,
            expected_interval_s=payload.expected_interval_s,
            expected_symbols=payload.expected_symbols,
            min_coverage_ratio=payload.min_coverage_ratio,
            symbols=payload.symbols,
            asset_type=payload.asset_type,
            start_ms=payload.start_ms,
            end_ms=payload.end_ms,
            trading_dates=payload.trading_dates,
            max_source_age_ms=payload.max_source_age_ms,
        )
    except SecondReplayDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sample")
def sample_second_quotes(
    payload: SecondQuoteSampleRequest,
    request: Request,
) -> dict:
    _guard_range(payload.start, payload.end, _MAX_SAMPLE_DAYS)
    try:
        frame = _replay(request).sample(
            payload.start,
            payload.end,
            symbols=payload.symbols,
            limit=payload.limit + 1,
        )
    except SecondReplayDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    truncated = frame.height > payload.limit
    frame = frame.head(payload.limit)
    rows = frame.to_dicts()
    for row in rows:
        # Nanosecond-derived IDs exceed JavaScript's exact integer range.
        row["capture_id"] = str(row["capture_id"])
    return {
        "rows": rows,
        "count": frame.height,
        "truncated": truncated,
    }
