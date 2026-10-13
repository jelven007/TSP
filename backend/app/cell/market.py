"""Read-only platform market gateway; Cells never open source-market connections."""

from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import AwareDatetime, BaseModel, Field

from app.cell.context import AccessError


class MarketQuote(BaseModel):
    symbol: str
    name: str = ""
    price: float = Field(gt=0, allow_inf_nan=False)
    change_pct: float = Field(allow_inf_nan=False)
    source_time: AwareDatetime
    sequence: int = Field(ge=0)
    trading: bool
    suspended: bool
    limit_up: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    limit_down: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    limits_known: bool = False

    def fresh(self) -> bool:
        age = (datetime.now(UTC) - self.source_time).total_seconds()
        return self.trading and not self.suspended and -2 <= age <= 15

    def tradable(self) -> bool:
        return self.fresh() and self.limits_known


class MarketGateway(Protocol):
    def quotes(self, symbols: list[str]) -> dict[str, MarketQuote]: ...
    def history(self, as_of: str, symbols: list[str], bars: int) -> dict: ...


class HttpMarketGateway:
    def __init__(self, url: str, token: str):
        self.client = httpx.Client(
            base_url=url.rstrip("/"), headers={"Authorization": "Bearer " + token},
            timeout=10, follow_redirects=False,
        )

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = self.client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AccessError(503, "market_unavailable") from exc

    def quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        try:
            result = self._get("/internal/cell-market/quotes", {"symbols": ",".join(symbols)})
            return {row["symbol"]: MarketQuote.model_validate(row) for row in result["quotes"]
                    if row["symbol"] in symbols}
        except (KeyError, TypeError, ValueError) as exc:
            raise AccessError(503, "market_unavailable") from exc

    def history(self, as_of: str, symbols: list[str], bars: int) -> dict:
        return self._get("/internal/cell-market/history", {
            "as_of": as_of, "symbols": ",".join(symbols), "bars": bars,
        })

    def close(self) -> None:
        self.client.close()


class UnavailableMarketGateway:
    def quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        raise AccessError(503, "market_unavailable")

    def history(self, as_of: str, symbols: list[str], bars: int) -> dict:
        raise AccessError(503, "market_unavailable")
