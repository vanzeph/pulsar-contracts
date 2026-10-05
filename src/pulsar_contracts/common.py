"""Shared primitives for Pulsar port contracts and domain objects.

This module holds the value types, enums and conventions that both the
market-data side and the execution side of the contract library depend on:

* every domain object is an immutable (frozen) pydantic model;
* every timestamp is timezone-aware and normalized to Asia/Shanghai;
* identifiers are lightweight new types so ports stay self-documenting.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, NewType, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from zoneinfo import ZoneInfo

__all__ = [
    "OrderId",
    "Subscription",
    "ContractModel",
    "Timestamp",
    "SHANGHAI_TZ",
    "Freq",
    "AdjustMode",
    "DataQuality",
    "Side",
    "PriceMode",
    "TimeInForce",
    "IdempotencyKey",
]

#: All Pulsar timestamps are expressed in this timezone.
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

#: Venue-confirmed order identifier handed out by an execution port.
OrderId = NewType("OrderId", str)


def _normalize_timestamp(value: datetime) -> datetime:
    """Return ``value`` as an aware datetime in Asia/Shanghai.

    Naive datetimes are interpreted as Asia/Shanghai wall time; aware
    datetimes are converted. This gives every timestamp in the system one
    canonical representation.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


#: Datetime type used by all domain objects; see :func:`_normalize_timestamp`.
Timestamp = Annotated[datetime, AfterValidator(_normalize_timestamp)]


class ContractModel(BaseModel):
    """Base class for all immutable Pulsar domain objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Freq(enum.StrEnum):
    """Bar frequencies understood by :class:`~pulsar_contracts.market_data.MarketDataPort`."""

    MINUTE = "1m"
    DAILY = "1d"


class AdjustMode(enum.StrEnum):
    """Adjustment mode derived at query time; the lake stores raw prices + factors."""

    RAW = "none"
    FORWARD = "forward"
    BACKWARD = "backward"


class DataQuality(enum.StrEnum):
    """Per-partition / per-row quality mark attached to stored data."""

    OK = "ok"
    BACKFILLED = "backfilled"
    SUSPECT = "suspect"


class Side(enum.StrEnum):
    """Order direction."""

    BUY = "buy"
    SELL = "sell"


class PriceMode(enum.StrEnum):
    """Price modes available for order intents.

    A-shares have no classic market order, so marketable semantics are
    expressed as counter-party price or best-five immediate-or-cancel.
    """

    LIMIT = "limit"
    COUNTER_PRICE = "counter_price"
    FIVE_LEVEL_CANCEL_REMAINDER = "five_level_cancel_remainder"


class TimeInForce(enum.StrEnum):
    """Validity period of an order intent."""

    DAY = "day"
    GTC = "gtc"


class IdempotencyKey(ContractModel):
    """Idempotency key of an order intent: ``run id`` + per-run sequence number.

    Re-submitting the same key must yield the same
    :data:`~pulsar_contracts.common.OrderId`; retries and replays never
    produce duplicate orders at the venue.
    """

    run_id: str = Field(min_length=1)
    seq: int = Field(ge=1)

    def to_str(self) -> str:
        """Canonical wire form ``"<run_id>:<seq>"``."""
        return f"{self.run_id}:{self.seq}"


class Subscription(Protocol):
    """Handle to an active snapshot subscription.

    Implementations returned by
    :meth:`~pulsar_contracts.market_data.MarketDataPort.subscribe` must expose
    this interface; unsubscribing is idempotent.
    """

    def unsubscribe(self) -> None: ...
