"""Execution domain objects and the ``ExecutionPort`` contract.

Execution-side semantics (defined by the Pulsar architecture baseline):

* ``OrderIntent`` is channel-agnostic: direction, symbol, quantity, price
  mode, time in force and an idempotency key. Board-lot rounding, funding
  checks and fee computation happen at the venue/gateway, never in the
  strategy.
* The order state machine is shared by every channel (backtest venue, paper
  broker, live gateway)::

      Created -> Submitted -> PartiallyFilled -> Filled
             -> Rejected        -> Cancelled

* ``submit`` is idempotent: re-submitting the same idempotency key returns
  the same :class:`~pulsar_contracts.common.OrderId`.
* Channels report progress exclusively through
  :class:`~pulsar_contracts.execution.ExecutionEvent` callbacks registered
  via ``on_event``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Callable, Protocol, runtime_checkable

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    IdempotencyKey,
    OrderId,
    PriceMode,
    Side,
    TimeInForce,
    Timestamp,
)

__all__ = [
    "OrderStatus",
    "ExecutionEventType",
    "OrderIntent",
    "Order",
    "Fill",
    "Position",
    "ExecutionEvent",
    "OrderState",
    "CancelResult",
    "ExecutionPort",
]


class OrderStatus(enum.StrEnum):
    """States of the shared order state machine.

    Terminal states are ``FILLED``, ``CANCELLED`` and ``REJECTED``; channels
    must map any private intermediate state onto this set before reporting.
    """

    CREATED = "created"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


class ExecutionEventType(enum.StrEnum):
    """Kinds of event pushed to ``ExecutionPort.on_event`` callbacks."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIAL_FILL = "partial_fill"
    FILL = "fill"
    CANCELLED = "cancelled"
    ERROR = "error"


class OrderIntent(ContractModel):
    """Channel-agnostic order intent produced by the core engine.

    Carries the idempotency key (``run id`` + sequence); lot-size rounding
    and funding checks happen downstream at the venue/gateway.
    """

    idempotency_key: IdempotencyKey
    side: Side
    symbol: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    price_mode: PriceMode
    limit_price: float | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = TimeInForce.DAY

    @model_validator(mode="after")
    def _validate_price_mode(self) -> "OrderIntent":
        if self.price_mode is PriceMode.LIMIT and self.limit_price is None:
            raise ValueError("PriceMode.LIMIT requires a limit_price")
        if self.price_mode is not PriceMode.LIMIT and self.limit_price is not None:
            raise ValueError("limit_price is only valid together with PriceMode.LIMIT")
        return self


class Order(ContractModel):
    """Venue-confirmed order mirroring the originating intent's fields."""

    order_id: OrderId
    idempotency_key: IdempotencyKey
    side: Side
    symbol: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    price_mode: PriceMode
    limit_price: float | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = TimeInForce.DAY
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: int = Field(default=0, ge=0)
    avg_fill_price: float | None = Field(default=None, gt=0)
    reject_reason: str | None = None
    created_at: Timestamp
    updated_at: Timestamp

    @classmethod
    def from_intent(
        cls, order_id: OrderId, intent: OrderIntent, created_at: datetime
    ) -> "Order":
        """Build a freshly-created order mirroring ``intent``."""
        return cls(
            order_id=order_id,
            idempotency_key=intent.idempotency_key,
            side=intent.side,
            symbol=intent.symbol,
            quantity=intent.quantity,
            price_mode=intent.price_mode,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force,
            status=OrderStatus.CREATED,
            created_at=created_at,
            updated_at=created_at,
        )

    @model_validator(mode="after")
    def _validate_invariants(self) -> "Order":
        if self.filled_quantity > self.quantity:
            raise ValueError(
                f"filled_quantity ({self.filled_quantity}) exceeds order quantity "
                f"({self.quantity})"
            )
        if self.filled_quantity > 0 and self.avg_fill_price is None:
            raise ValueError("avg_fill_price is required once filled_quantity > 0")
        if self.status is OrderStatus.REJECTED and not self.reject_reason:
            raise ValueError("reject_reason is required when status is REJECTED")
        if self.status is not OrderStatus.REJECTED and self.reject_reason is not None:
            raise ValueError("reject_reason may only be set when status is REJECTED")
        return self


class Fill(ContractModel):
    """Fill report confirmed by the execution port, with per-fill fees.

    Fees (commission both ways, stamp duty on sells, transfer fee) are
    computed and booked per fill by the venue; rates come from run
    configuration, never from this contract.
    """

    fill_id: str = Field(min_length=1)
    order_id: OrderId
    symbol: str = Field(min_length=1)
    side: Side
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    commission: float = Field(default=0.0, ge=0)
    stamp_duty: float = Field(default=0.0, ge=0)
    transfer_fee: float = Field(default=0.0, ge=0)
    ts: Timestamp


class Position(ContractModel):
    """Position distinguishing total holdings from T+1 sellable quantity.

    Shares bought today are not available for sale until the next trading
    day, hence ``available_quantity <= quantity``.
    """

    symbol: str = Field(min_length=1)
    quantity: int = Field(ge=0)
    available_quantity: int = Field(ge=0)
    avg_cost: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_availability(self) -> "Position":
        if self.available_quantity > self.quantity:
            raise ValueError(
                f"available_quantity ({self.available_quantity}) exceeds quantity "
                f"({self.quantity})"
            )
        return self


class ExecutionEvent(ContractModel):
    """Event pushed to ``ExecutionPort.on_event`` callbacks.

    Fill events must carry the :class:`Fill`; rejection/error events must
    carry a human-readable ``reason``; the final fill that completes an
    order is reported as ``FILL`` (earlier partial executions as
    ``PARTIAL_FILL``).
    """

    event_type: ExecutionEventType
    order_id: OrderId
    ts: Timestamp
    fill: Fill | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_payload(self) -> "ExecutionEvent":
        if self.event_type in (ExecutionEventType.PARTIAL_FILL, ExecutionEventType.FILL):
            if self.fill is None:
                raise ValueError(f"{self.event_type} events require a fill payload")
            if self.fill.order_id != self.order_id:
                raise ValueError("fill payload order_id does not match the event order_id")
        elif self.fill is not None:
            raise ValueError("fill payload is only valid on PARTIAL_FILL/FILL events")
        if self.event_type in (ExecutionEventType.REJECTED, ExecutionEventType.ERROR) and not (
            self.reason
        ):
            raise ValueError(f"{self.event_type} events require a reason")
        return self


class OrderState(ContractModel):
    """Compact order snapshot returned by ``ExecutionPort.query``."""

    order_id: OrderId
    status: OrderStatus
    filled_quantity: int = Field(ge=0)
    avg_fill_price: float | None = Field(default=None, gt=0)
    updated_at: Timestamp


class CancelResult(ContractModel):
    """Outcome of a cancel request.

    ``accepted`` reflects whether the venue accepted the cancellation; a
    rejected cancellation is typically caused by the order already being
    filled. Unconfirmed cancels must be reconciled via ``query``.
    """

    order_id: OrderId
    accepted: bool
    status: OrderStatus | None = None
    reason: str | None = None


@runtime_checkable
class ExecutionPort(Protocol):
    """Trading port: the only way the core engine submits orders and reads fills.

    All channels (backtest venue, paper broker, live gateway) implement this
    protocol against the shared order state machine; strategy code never
    changes when the channel changes.
    """

    def submit(self, intent: OrderIntent) -> OrderId:
        """Submit an order intent.

        Idempotent: re-submitting the same idempotency key returns the same
        :class:`~pulsar_contracts.common.OrderId` without creating a new
        order at the venue.
        """
        ...

    def cancel(self, order_id: OrderId) -> CancelResult:
        """Request cancellation of an active order."""
        ...

    def query(self, order_id: OrderId) -> OrderState:
        """Query the current state of an order (used for reconciliation)."""
        ...

    def positions(self) -> list[Position]:
        """Return current positions, one per held symbol."""
        ...

    def on_event(self, callback: Callable[[ExecutionEvent], None]) -> None:
        """Register a callback receiving all execution events of this port."""
        ...
