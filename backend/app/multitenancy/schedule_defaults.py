"""Portable settings for the existing Platform and tenant schedulers."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ClockText = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Provider = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
PositiveSeconds = Annotated[int, Field(ge=1, le=86400)]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CloseClock(Settings):
    hour: int = Field(ge=15, le=23)
    minute: int = Field(ge=0, le=59)


class ReviewSchedule(CloseClock):
    enabled: bool


class MiningSchedule(Settings):
    mining_schedule_enabled: bool
    mining_schedule_weekday: int = Field(ge=0, le=4)
    mining_budget_profile: Literal["balanced", "strict"]


class KlineSchedule(Settings):
    enabled: bool
    interval_seconds: PositiveSeconds
    windows: list[list[ClockText]]
    time: ClockText


class PlatformSchedule(Settings):
    data_sync_auto_enabled: bool | None = None
    realtime_quotes_enabled: bool | None = None
    realtime_quote_interval: float | None = Field(default=None, gt=0, le=86400)
    realtime_pull_stock: bool | None = None
    second_quote_archive_enabled: bool | None = None
    second_quote_archive_window: Literal["full", "preopen"] | None = None
    second_quote_archive_retention_days: int | None = Field(default=None, ge=0, le=365)
    minute_sync_enabled: bool | None = None
    minute_sync_days: int | None = Field(default=None, ge=1, le=3650)
    minute_sync_segment_days: int | None = Field(default=None, ge=1, le=3650)
    minute_refresh_enabled: bool | None = None
    minute_refresh_interval: PositiveSeconds | None = None
    minute_intraday_refresh: bool | None = None
    minute_intraday_refresh_interval: PositiveSeconds | None = None
    depth_finalize_time: CloseClock | None = None
    depth_polling_interval: float | None = Field(default=None, ge=1, le=600)
    limit_ladder_monitor_enabled: bool | None = None
    pipeline_regime_enabled: bool | None = None
    pipeline_pull_a_share: bool | None = None
    pipeline_pull_index: bool | None = None
    pipeline_index_symbols: str | None = Field(default=None, max_length=50000)
    daily_data_provider: Provider | None = None
    adj_factor_provider: Provider | None = None
    corporate_actions_provider: Provider | None = None
    minute_data_provider: Provider | None = None
    depth5_data_provider: Provider | None = None
    realtime_data_provider: Provider | None = None
    financial_data_provider: Provider | None = None
    kline_sync_1m: KlineSchedule | None = None
    kline_sync_5m: KlineSchedule | None = None
    kline_sync_10m: KlineSchedule | None = None
    kline_sync_15m: KlineSchedule | None = None
    kline_sync_1d: KlineSchedule | None = None
    kline_sync_1w: KlineSchedule | None = None

    @model_validator(mode="after")
    def validate_clocks(self):
        from app.services.kline_periods import DEFAULT_WINDOWS, PERIOD_WINDOWS, TABLES

        if self.depth_finalize_time is not None:
            clock = self.depth_finalize_time
            if not 901 <= clock.hour * 60 + clock.minute <= 1080:
                raise ValueError("depth finalization must be between 15:01 and 18:00")
        for period in TABLES:
            value = getattr(self, f"kline_sync_{period}")
            if value is None:
                continue
            if value.windows != PERIOD_WINDOWS.get(period, DEFAULT_WINDOWS):
                raise ValueError("invalid K-line time windows")
            fixed = {"1d": "15:35", "1w": "09:00"}.get(period)
            if fixed and value.time != fixed:
                raise ValueError("invalid fixed K-line schedule")
        return self


class ScheduleDefaults(Settings):
    platform: PlatformSchedule
    review: ReviewSchedule
    mining: MiningSchedule
    review_push_mode: Literal["manual", "auto"]
