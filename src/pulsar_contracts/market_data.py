"""Market-data domain objects and the ``MarketDataPort`` contract.

Data-side semantics (defined by the Pulsar architecture baseline):

* ``fetch_*`` reads the local data lake — synchronous, deterministic,
  reproducible. Missing ranges raise instead of silently returning partial
  data.
* ``subscribe`` serves Paper/Live realtime snapshots — best effort; late or
  missing snapshots are flagged by sequence number, never raised.
* The lake stores raw prices plus adjustment factors; forward/backward
  adjustment is derived at query time according to ``AdjustMode``.
* Bar timestamps follow the left-closed convention: a daily bar carries
  ``00:00`` of its trading day in Asia/Shanghai.
"""

from __future__ import annotations

import enum
from datetime import date
from typing import Callable, Protocol, runtime_checkable

from pandas import DataFrame
from pydantic import Field, model_validator

from .common import (
    AdjustMode,
    ContractModel,
    DataQuality,
    Freq,
    Subscription,
    Timestamp,
)

__all__ = [
    "Exchange",
    "Board",
    "InstrumentStatus",
    "Instrument",
    "Bar",
    "QuoteLevel",
    "Snapshot",
    "CorporateAction",
    "MarketDataPort",
]


class Exchange(enum.StrEnum):
    """Securities exchanges traded by Pulsar."""

    SSE = "SSE"  # Shanghai
    SZSE = "SZSE"  # Shenzhen
    BSE = "BSE"  # Beijing


class Board(enum.StrEnum):
    """Listing boards; drives price-limit rules (main ±10%, GEM/STAR ±20%, ST ±5%)."""

    MAIN = "main"
    GEM = "gem"  # 创业板
    STAR = "star"  # 科创板
    BSE = "bse"  # 北交所


class InstrumentStatus(enum.StrEnum):
    """Listing state of an instrument as of the query date."""

    LISTED = "listed"
    SUSPENDED = "suspended"
    DELISTED = "delisted"


class Instrument(ContractModel):
    """Instrument metadata: symbol, exchange, board, listing state, share capital."""

    symbol: str = Field(min_length=1)
    name: str = ""
    exchange: Exchange
    board: Board
    is_st: bool = False
    status: InstrumentStatus = InstrumentStatus.LISTED
    list_date: date
    delist_date: date | None = None
    shares_outstanding: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> "Instrument":
        if self.delist_date is not None and self.delist_date < self.list_date:
            raise ValueError("delist_date must not precede list_date")
        return self


class Bar(ContractModel):
    """OHLCV bar with turnover and adjustment factor.

    ``ts`` is the left-closed interval start (daily bars carry ``00:00`` of
    the trading day, Asia/Shanghai); ``volume`` is in shares and ``amount``
    in CNY.
    """

    symbol: str = Field(min_length=1)
    ts: Timestamp
    freq: Freq
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    amount: float = Field(ge=0)
    adjust_factor: float = Field(default=1.0, gt=0)
    quality: DataQuality = DataQuality.OK

    @model_validator(mode="after")
    def _validate_ohlc(self) -> "Bar":
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(
                "open/close must lie within [low, high] "
                f"(o={self.open}, h={self.high}, l={self.low}, c={self.close})"
            )
        return self


class QuoteLevel(ContractModel):
    """One price level of an order-book snapshot."""

    price: float = Field(gt=0)
    volume: float = Field(ge=0)


class Snapshot(ContractModel):
    """Realtime quote snapshot with best-five order-book levels.

    ``seq`` is a per-symbol monotonically increasing sequence number:
    consumers detect late or missing snapshots by gaps in ``seq`` — delivery
    gaps never raise. ``volume``/``amount`` are cumulative day totals.
    """

    symbol: str = Field(min_length=1)
    ts: Timestamp
    seq: int = Field(ge=0)
    last_price: float = Field(gt=0)
    volume: float = Field(ge=0)
    amount: float = Field(ge=0)
    bids: tuple[QuoteLevel, ...] = ()  # index 0 = best bid, at most five levels
    asks: tuple[QuoteLevel, ...] = ()  # index 0 = best ask, at most five levels

    @model_validator(mode="after")
    def _validate_book_depth(self) -> "Snapshot":
        if len(self.bids) > 5 or len(self.asks) > 5:
            raise ValueError(
                f"order book carries at most five levels per side "
                f"(bids={len(self.bids)}, asks={len(self.asks)})"
            )
        return self


class CorporateAction(ContractModel):
    """Corporate action (cash dividend, bonus shares, rights issue) per ex-date.

    Ratios are per share: ``bonus_share_ratio=0.1`` means "10 bonus shares
    per 100 held" (10送1). These records drive adjustment-factor computation.
    """

    symbol: str = Field(min_length=1)
    ex_date: date
    cash_dividend_per_share: float = Field(default=0.0, ge=0)
    bonus_share_ratio: float = Field(default=0.0, ge=0)
    rights_issue_ratio: float = Field(default=0.0, ge=0)
    rights_issue_price: float | None = Field(default=None, gt=0)
    description: str = ""

    @model_validator(mode="after")
    def _validate_components(self) -> "CorporateAction":
        if self.rights_issue_ratio > 0 and self.rights_issue_price is None:
            raise ValueError("a rights issue requires rights_issue_price")
        if (
            self.cash_dividend_per_share == 0
            and self.bonus_share_ratio == 0
            and self.rights_issue_ratio == 0
        ):
            raise ValueError("a corporate action must carry at least one non-zero component")
        return self


@runtime_checkable
class MarketDataPort(Protocol):
    """Data port: the only way the core engine reads market and reference data.

    ``fetch_*``/``calendar``/``list_instruments`` read the local data lake
    (deterministic, reproducible); ``subscribe`` streams best-effort
    snapshots for Paper/Live. Implementations live outside this library.
    """

    def list_instruments(self, as_of: date) -> list[Instrument]:
        """List instruments known to the lake as of ``as_of``."""
        ...

    def fetch_bars(
        self,
        symbols: list[str],
        start: date,
        end: date,
        freq: Freq,
        adjust: AdjustMode,
    ) -> DataFrame:
        """Return bars as a DataFrame with the canonical bar columns:

        ``symbol, ts, open, high, low, close, volume, amount,
        adjust_factor, quality`` — one row per symbol and bar interval,
        indexed by nothing in particular (never rely on row order).
        Inclusive on both ``start`` and ``end``; missing in-range trading
        days must raise rather than return partial data.
        """
        ...

    def fetch_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Return all corporate actions on record for ``symbol``."""
        ...

    def calendar(self, start: date, end: date) -> list[date]:
        """Return trading days in ``[start, end]``, ascending."""
        ...

    def subscribe(
        self, symbols: list[str], on_snapshot: Callable[[Snapshot], None]
    ) -> Subscription:
        """Start streaming snapshots for ``symbols``.

        Best effort: late or missing snapshots are flagged through
        ``Snapshot.seq`` gaps and never raise. Use the returned
        :class:`~pulsar_contracts.common.Subscription` to stop the stream.
        """
        ...
