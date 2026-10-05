"""Tests for execution domain objects and the ExecutionPort protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

import pytest
from pydantic import ValidationError

from pulsar_contracts import (
    SHANGHAI_TZ,
    CancelResult,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionPort,
    Fill,
    IdempotencyKey,
    Order,
    OrderId,
    OrderIntent,
    OrderState,
    OrderStatus,
    Position,
    PriceMode,
    Side,
    TimeInForce,
)


class FakeExecution:
    """Minimal structural implementation proving the protocol is satisfiable."""

    def submit(self, intent: OrderIntent) -> OrderId:
        return OrderId("ord-1")

    def cancel(self, order_id: OrderId) -> CancelResult:
        return CancelResult(order_id=order_id, accepted=False)

    def query(self, order_id: OrderId) -> OrderState:
        return OrderState(
            order_id=order_id,
            status=OrderStatus.CREATED,
            filled_quantity=0,
            updated_at=datetime(2026, 10, 5, 9, 30),
        )

    def positions(self) -> list[Position]:
        return []

    def on_event(self, callback: Callable[[ExecutionEvent], None]) -> None:
        return None


class TestExecutionPortProtocol:
    def test_fake_implements_port(self) -> None:
        port: ExecutionPort = FakeExecution()
        assert isinstance(port, ExecutionPort)

    def test_protocol_is_runtime_checkable(self) -> None:
        class NotAPort:
            pass

        assert not isinstance(NotAPort(), ExecutionPort)


class TestOrderStatus:
    def test_terminal_states(self) -> None:
        terminal = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
        for status in OrderStatus:
            assert status.is_terminal == (status in terminal)

    def test_state_machine_covers_design_transitions(self) -> None:
        assert {status.value for status in OrderStatus} == {
            "created",
            "submitted",
            "partially_filled",
            "filled",
            "cancelled",
            "rejected",
        }


class TestOrderIntent:
    def make(self, **overrides: object) -> OrderIntent:
        payload: dict[str, object] = {
            "idempotency_key": IdempotencyKey(run_id="run-1", seq=1),
            "side": Side.BUY,
            "symbol": "SH600519",
            "quantity": 200,
            "price_mode": PriceMode.LIMIT,
            "limit_price": 1500.0,
        }
        payload.update(overrides)
        return OrderIntent.model_validate(payload)

    def test_valid_limit_intent(self) -> None:
        intent = self.make()
        assert intent.time_in_force is TimeInForce.DAY

    def test_limit_requires_price(self) -> None:
        with pytest.raises(ValidationError):
            self.make(limit_price=None)

    def test_price_only_valid_for_limit(self) -> None:
        with pytest.raises(ValidationError):
            self.make(price_mode=PriceMode.COUNTER_PRICE)

    def test_counter_price_intent(self) -> None:
        intent = self.make(price_mode=PriceMode.COUNTER_PRICE, limit_price=None)
        assert intent.limit_price is None

    def test_non_positive_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make(quantity=0)

    def test_no_lot_size_constraint_at_intent_level(self) -> None:
        # Board-lot rounding is a venue concern; odd lots are representable.
        assert self.make(quantity=157).quantity == 157


class TestOrder:
    def make_intent(self) -> OrderIntent:
        return OrderIntent(
            idempotency_key=IdempotencyKey(run_id="run-1", seq=3),
            side=Side.SELL,
            symbol="SZ300750",
            quantity=100,
            price_mode=PriceMode.LIMIT,
            limit_price=200.0,
        )

    def test_from_intent_mirrors_fields(self) -> None:
        now = datetime(2026, 10, 5, 14, 55)
        order = Order.from_intent(OrderId("ord-9"), self.make_intent(), now)
        assert order.order_id == "ord-9"
        assert order.symbol == "SZ300750"
        assert order.side is Side.SELL
        assert order.limit_price == 200.0
        assert order.status is OrderStatus.CREATED
        assert order.filled_quantity == 0
        expected_ts = now.replace(tzinfo=SHANGHAI_TZ)
        assert order.created_at == order.updated_at == expected_ts

    def make_order(self, **overrides: object) -> Order:
        now = datetime(2026, 10, 5, 9, 30)
        base = Order.from_intent(OrderId("ord-1"), self.make_intent(), now)
        return Order.model_validate({**base.model_dump(), **overrides})

    def test_overfill_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make_order(filled_quantity=101)

    def test_partial_fill_requires_avg_price(self) -> None:
        with pytest.raises(ValidationError):
            self.make_order(
                status=OrderStatus.PARTIALLY_FILLED,
                filled_quantity=50,
                avg_fill_price=None,
            )

    def test_partial_fill_with_avg_price(self) -> None:
        order = self.make_order(
            status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=50,
            avg_fill_price=199.5,
        )
        assert order.avg_fill_price == 199.5

    def test_rejected_requires_reason(self) -> None:
        with pytest.raises(ValidationError):
            self.make_order(status=OrderStatus.REJECTED)

    def test_reason_only_when_rejected(self) -> None:
        order = self.make_order(status=OrderStatus.REJECTED, reject_reason="insufficient funds")
        assert order.reject_reason == "insufficient funds"
        with pytest.raises(ValidationError):
            self.make_order(status=OrderStatus.SUBMITTED, reject_reason="late")


class TestFill:
    def make(self, **overrides: object) -> Fill:
        payload: dict[str, object] = {
            "fill_id": "fill-1",
            "order_id": OrderId("ord-1"),
            "symbol": "SH600519",
            "side": Side.BUY,
            "price": 1500.0,
            "quantity": 100,
            "ts": datetime(2026, 10, 5, 9, 31),
        }
        payload.update(overrides)
        return Fill.model_validate(payload)

    def test_fee_defaults_zero(self) -> None:
        fill = self.make()
        assert (fill.commission, fill.stamp_duty, fill.transfer_fee) == (0.0, 0.0, 0.0)

    def test_fee_breakdown_round_trip(self) -> None:
        fill = self.make(
            commission=5.0,
            stamp_duty=0.0,
            transfer_fee=0.15,
        )
        assert Fill.model_validate(fill.model_dump()) == fill

    def test_non_positive_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make(price=0.0)


class TestPosition:
    def test_t_plus_one_availability(self) -> None:
        pos = Position(symbol="SH600519", quantity=300, available_quantity=200, avg_cost=1480.0)
        assert pos.available_quantity <= pos.quantity

    def test_availability_over_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Position(symbol="SH600519", quantity=100, available_quantity=200)

    def test_empty_position(self) -> None:
        pos = Position(symbol="SH600519", quantity=0, available_quantity=0)
        assert pos.avg_cost is None


class TestExecutionEvent:
    def make_fill(self, order_id: OrderId = OrderId("ord-1")) -> Fill:
        return Fill(
            fill_id="fill-1",
            order_id=order_id,
            symbol="SH600519",
            side=Side.BUY,
            price=1500.0,
            quantity=100,
            ts=datetime(2026, 10, 5, 9, 31),
        )

    def test_fill_event_carries_fill(self) -> None:
        event = ExecutionEvent(
            event_type=ExecutionEventType.FILL,
            order_id=OrderId("ord-1"),
            ts=datetime(2026, 10, 5, 9, 31),
            fill=self.make_fill(),
        )
        assert event.fill is not None and event.fill.quantity == 100

    def test_fill_event_requires_fill_payload(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionEvent(
                event_type=ExecutionEventType.PARTIAL_FILL,
                order_id=OrderId("ord-1"),
                ts=datetime(2026, 10, 5, 9, 31),
            )

    def test_fill_payload_must_match_order(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionEvent(
                event_type=ExecutionEventType.FILL,
                order_id=OrderId("ord-1"),
                ts=datetime(2026, 10, 5, 9, 31),
                fill=self.make_fill(OrderId("ord-other")),
            )

    def test_rejected_requires_reason(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionEvent(
                event_type=ExecutionEventType.REJECTED,
                order_id=OrderId("ord-1"),
                ts=datetime(2026, 10, 5, 9, 31),
            )

    def test_error_event_with_reason(self) -> None:
        event = ExecutionEvent(
            event_type=ExecutionEventType.ERROR,
            order_id=OrderId("ord-1"),
            ts=datetime(2026, 10, 5, 9, 31),
            reason="gateway disconnected",
        )
        assert event.reason == "gateway disconnected"

    def test_fill_payload_invalid_on_accepted(self) -> None:
        with pytest.raises(ValidationError):
            ExecutionEvent(
                event_type=ExecutionEventType.ACCEPTED,
                order_id=OrderId("ord-1"),
                ts=datetime(2026, 10, 5, 9, 30),
                fill=self.make_fill(),
            )

    def test_event_type_vocabulary(self) -> None:
        assert {t.value for t in ExecutionEventType} == {
            "accepted",
            "rejected",
            "partial_fill",
            "fill",
            "cancelled",
            "error",
        }


class TestCancelResultAndOrderState:
    def test_cancel_result_shapes(self) -> None:
        rejected = CancelResult(
            order_id=OrderId("ord-1"),
            accepted=False,
            status=OrderStatus.FILLED,
            reason="already filled",
        )
        assert rejected.status is OrderStatus.FILLED

    def test_order_state_shape(self) -> None:
        state = OrderState(
            order_id=OrderId("ord-1"),
            status=OrderStatus.SUBMITTED,
            filled_quantity=0,
            updated_at=datetime(2026, 10, 5, 9, 30),
        )
        assert state.avg_fill_price is None
