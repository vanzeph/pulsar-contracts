"""Tests for shared primitives: enums, idempotency key, timestamps, immutability."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from pulsar_contracts import (
    SHANGHAI_TZ,
    AdjustMode,
    Bar,
    DataQuality,
    Freq,
    IdempotencyKey,
    OrderId,
    PriceMode,
    Side,
    TimeInForce,
)


def make_bar(**overrides: object) -> Bar:
    payload: dict[str, object] = {
        "symbol": "SH600519",
        "ts": datetime(2026, 9, 30, 0, 0),
        "freq": Freq.DAILY,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "volume": 1_000_000.0,
        "amount": 10_500_000.0,
    }
    payload.update(overrides)
    return Bar.model_validate(payload)


class TestEnums:
    def test_freq_values_match_lake_partitions(self) -> None:
        assert Freq.DAILY == "1d"
        assert Freq.MINUTE == "1m"
        assert Freq.MINUTE_5 == "5m"
        assert Freq.MINUTE_15 == "15m"
        assert Freq.MINUTE_30 == "30m"
        assert Freq.MINUTE_60 == "60m"

    def test_freq_members_unique(self) -> None:
        values = [freq.value for freq in Freq]
        assert len(values) == len(set(values))

    def test_adjust_modes(self) -> None:
        assert {AdjustMode.RAW, AdjustMode.FORWARD, AdjustMode.BACKWARD} == {
            AdjustMode("none"),
            AdjustMode("forward"),
            AdjustMode("backward"),
        }

    def test_data_quality_marks(self) -> None:
        assert [mode.value for mode in DataQuality] == ["ok", "backfilled", "suspect"]

    def test_side_and_time_in_force(self) -> None:
        assert Side.BUY == "buy"
        assert Side.SELL == "sell"
        assert TimeInForce.DAY == "day"
        assert TimeInForce.GTC == "gtc"

    def test_price_modes(self) -> None:
        assert PriceMode.LIMIT == "limit"
        assert PriceMode.COUNTER_PRICE == "counter_price"
        assert PriceMode.FIVE_LEVEL_CANCEL_REMAINDER == "five_level_cancel_remainder"


class TestIdempotencyKey:
    def test_value_semantics(self) -> None:
        key = IdempotencyKey(run_id="run-20261005", seq=1)
        assert key == IdempotencyKey(run_id="run-20261005", seq=1)
        assert key != IdempotencyKey(run_id="run-20261005", seq=2)
        assert hash(key) == hash(IdempotencyKey(run_id="run-20261005", seq=1))

    def test_wire_form(self) -> None:
        key = IdempotencyKey(run_id="alpha", seq=42)
        assert key.to_str() == "alpha:42"
        assert str(key) == "run_id='alpha' seq=42" or "alpha" in str(key)

    def test_invalid_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IdempotencyKey(run_id="", seq=1)
        with pytest.raises(ValidationError):
            IdempotencyKey(run_id="alpha", seq=0)


class TestTimestampNormalization:
    def test_naive_datetime_interpreted_as_shanghai(self) -> None:
        bar = make_bar(ts=datetime(2026, 9, 30, 0, 0))
        assert bar.ts.tzinfo is not None
        assert bar.ts.utcoffset() == SHANGHAI_TZ.utcoffset(bar.ts)
        assert (bar.ts.year, bar.ts.month, bar.ts.day) == (2026, 9, 30)

    def test_aware_datetime_converted_to_shanghai(self) -> None:
        bar = make_bar(ts=datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc))
        assert bar.ts.hour == 0
        assert bar.ts.day == 30

    def test_date_fields_stay_dates(self) -> None:
        bar = make_bar()
        assert isinstance(bar.ts, datetime)


class TestImmutability:
    def test_domain_objects_are_frozen(self) -> None:
        bar = make_bar()
        with pytest.raises(ValidationError):
            bar.close = 99.0  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            make_bar(unknown_column=1)

    def test_order_id_is_str_newtype(self) -> None:
        oid = OrderId("ord-1")
        assert isinstance(oid, str)


class TestRoundTrip:
    def test_bar_serialization_round_trip(self) -> None:
        bar = make_bar(
            ts=datetime(2026, 9, 30),
            quality=DataQuality.BACKFILLED,
            adjust_factor=1.25,
        )
        dumped = bar.model_dump(mode="json")
        assert Bar.model_validate(dumped) == bar

    def test_date_serialization_is_iso(self) -> None:
        bar = make_bar()
        assert bar.model_dump(mode="json")["ts"].startswith("2026-09-30T00:00:00")
