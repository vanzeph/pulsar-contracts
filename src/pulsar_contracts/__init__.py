"""pulsar-contracts: the single public contract library of the Pulsar system.

Defines the two cross-repository ports (``MarketDataPort``,
``ExecutionPort``) and the immutable domain objects exchanged through them.
This package intentionally contains no implementation logic and depends only
on basic libraries (pydantic, pandas).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .common import (
    AdjustMode,
    ContractModel,
    DataQuality,
    Freq,
    IdempotencyKey,
    OrderId,
    PriceMode,
    SHANGHAI_TZ,
    Side,
    Subscription,
    TimeInForce,
    Timestamp,
)
from .execution import (
    CancelResult,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionPort,
    Fill,
    Order,
    OrderIntent,
    OrderState,
    OrderStatus,
    Position,
)
from .market_data import (
    Bar,
    Board,
    CorporateAction,
    Exchange,
    Instrument,
    InstrumentStatus,
    MarketDataPort,
    QuoteLevel,
    Snapshot,
)

try:
    __version__ = version("pulsar-contracts")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0.dev0"

__all__ = [
    "__version__",
    # common
    "AdjustMode",
    "ContractModel",
    "DataQuality",
    "Freq",
    "IdempotencyKey",
    "OrderId",
    "PriceMode",
    "SHANGHAI_TZ",
    "Side",
    "Subscription",
    "TimeInForce",
    "Timestamp",
    # market data
    "Bar",
    "Board",
    "CorporateAction",
    "Exchange",
    "Instrument",
    "InstrumentStatus",
    "MarketDataPort",
    "QuoteLevel",
    "Snapshot",
    # execution
    "CancelResult",
    "ExecutionEvent",
    "ExecutionEventType",
    "ExecutionPort",
    "Fill",
    "Order",
    "OrderIntent",
    "OrderState",
    "OrderStatus",
    "Position",
]
