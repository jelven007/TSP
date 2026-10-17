"""Company actions: amounts are yuan/share and shares/share, dates are explicit."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CorporateAction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    asset_type: Literal["stock", "etf"] = "stock"
    source: str = Field(min_length=1, max_length=80)
    ex_date: date
    record_date: date | None = None
    cash_per_share: float = Field(default=0, ge=0)
    cash_pay_date: date | None = None
    bonus_per_share: float = Field(default=0, ge=0)
    bonus_listing_date: date | None = None
    rights_per_share: float = Field(default=0, ge=0)
    rights_price: float | None = Field(default=None, ge=0)
    rights_start_date: date | None = None
    rights_end_date: date | None = None
    rights_listing_date: date | None = None
    dates_verified: bool = False
    evidence: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.record_date and self.record_date >= self.ex_date:
            raise ValueError("record_date must precede ex_date")
        for day in (self.cash_pay_date, self.bonus_listing_date, self.rights_listing_date):
            if day and day < self.ex_date:
                raise ValueError("payment/listing cannot precede ex_date")
        if self.rights_start_date and self.record_date and self.rights_start_date <= self.record_date:
            raise ValueError("rights subscription must follow record close")
        if self.rights_start_date and self.rights_end_date and self.rights_start_date > self.rights_end_date:
            raise ValueError("invalid rights subscription window")
        if self.rights_end_date and self.rights_listing_date and self.rights_end_date >= self.rights_listing_date:
            raise ValueError("rights listing must follow subscription")
        return self

    def missing_evidence(self) -> list[str]:
        required = ["record_date"]
        if self.cash_per_share:
            required.append("cash_pay_date")
        if self.bonus_per_share:
            required.append("bonus_listing_date")
        if self.rights_per_share:
            required += ["rights_price", "rights_start_date", "rights_end_date", "rights_listing_date"]
        missing = [key for key in required if getattr(self, key) is None]
        if not self.dates_verified or not self.evidence:
            missing.append("verified_date_evidence")
        return missing
