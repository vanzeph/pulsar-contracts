"""Tests for market-data domain objects and the MarketDataPort protocol."""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

import pytest
from pandas import DataFrame
from pydantic import ValidationError

from pulsar_contracts import (
    AdjustMode,
    Bar,
    Board,
    CorporateAction,
    DataQuality,
    Exchange,
    Freq,
    Instrument,
    InstrumentStatus,
    MarketDataPort,
    QuoteLevel,
    Snapshot,
    Subscription,
)


class FakeSubscription:
    def unsubscribe(self) -> None:
        return None


class FakeMarketData:
    """Minimal structural implementation proving the protocol is satisfiable."""

    def list_instruments(self, as_of: date) -> list[Instrument]:
        return []

    def fetch_bars(
        self,
        symbols: list[str],
        start: date,
        end: date,
        freq: Freq,
        adjust: AdjustMode,
    ) -> DataFrame:
        return DataFrame()

    def fetch_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        return []

    def calendar(self, start: date, end: date) -> list[date]:
        return []

    def subscribe(
        self, symbols: list[str], on_snapshot: Callable[[Snapshot], None]
    ) -> Subscription:
        return FakeSubscription()


class TestMarketDataPortProtocol:
    def test_fake_implements_port(self) -> None:
        port: MarketDataPort = FakeMarketData()
        assert isinstance(port, MarketDataPort)

    def test_protocol_is_runtime_checkable(self) -> None:
        class NotAPort:
            pass

        assert not isinstance(NotAPort(), MarketDataPort)


class TestInstrument:
    def make(self, **overrides: object) -> Instrument:
        payload: dict[str, object] = {
            "symbol": "SH600519",
            "name": "Kweichow Moutai",
            "exchange": Exchange.SSE,
            "board": Board.MAIN,
            "list_date": date(2001, 8, 27),
        }
        payload.update(overrides)
        return Instrument.model_validate(payload)

    def test_valid_instrument(self) -> None:
        inst = self.make()
        assert inst.status is InstrumentStatus.LISTED
        assert inst.is_st is False
        assert inst.delist_date is None

    def test_delist_before_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make(delist_date=date(2000, 1, 1))

    def test_st_flag_and_status(self) -> None:
        inst = self.make(is_st=True, status=InstrumentStatus.SUSPENDED)
        assert inst.is_st and inst.status is InstrumentStatus.SUSPENDED


class TestBar:
    def make(self, **overrides: object) -> Bar:
        payload: dict[str, object] = {
            "symbol": "SZ300750",
            "ts": datetime(2026, 9, 30, 0, 0),
            "freq": Freq.DAILY,
            "open": 100.0,
            "high": 110.0,
            "low": 98.0,
            "close": 105.0,
            "volume": 5_000_000.0,
            "amount": 520_000_000.0,
        }
        payload.update(overrides)
        return Bar.model_validate(payload)

    def test_valid_bar_defaults(self) -> None:
        bar = self.make()
        assert bar.adjust_factor == 1.0
        assert bar.quality is DataQuality.OK

    def test_ohlc_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            self.make(open=120.0)  # open above high
        with pytest.raises(ValidationError):
            self.make(close=90.0)  # close below low

    def test_non_positive_prices_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make(low=0.0)

    def test_zero_volume_allowed_suspended_style_bar(self) -> None:
        bar = self.make(volume=0.0, amount=0.0)
        assert bar.volume == 0.0

    def test_negative_adjust_factor_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make(adjust_factor=0.0)


class TestSnapshot:
    def make(self, **overrides: object) -> Snapshot:
        payload: dict[str, object] = {
            "symbol": "SH600519",
            "ts": datetime(2026, 10, 5, 9, 30, 0),
            "seq": 7,
            "last_price": 1500.0,
            "volume": 123_456.0,
            "amount": 185_284_000.0,
            "bids": [
                {"price": 1499.9, "volume": 12.0},
                {"price": 1499.8, "volume": 30.0},
            ],
            "asks": [{"price": 1500.1, "volume": 8.0}],
        }
        payload.update(overrides)
        return Snapshot.model_validate(payload)

    def test_book_levels_coerced_to_immutable_tuples(self) -> None:
        snap = self.make()
        assert isinstance(snap.bids, tuple)
        assert isinstance(snap.bids[0], QuoteLevel)
        assert snap.bids[0].price == 1499.9

    def test_more_than_five_levels_rejected(self) -> None:
        six_levels = [{"price": 10.0 - i, "volume": 1.0} for i in range(6)]
        with pytest.raises(ValidationError):
            self.make(bids=six_levels)

    def test_seq_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            self.make(seq=-1)

    def test_empty_book_allowed(self) -> None:
        snap = self.make(bids=[], asks=[])
        assert snap.bids == () and snap.asks == ()


class TestCorporateAction:
    def test_cash_dividend(self) -> None:
        action = CorporateAction(
            symbol="SH600519",
            ex_date=date(2026, 6, 20),
            cash_dividend_per_share=0.3,
        )
        assert action.bonus_share_ratio == 0.0

    def test_bonus_shares_ten_to_one(self) -> None:
        action = CorporateAction(
            symbol="SZ000001",
            ex_date=date(2026, 5, 10),
            bonus_share_ratio=0.1,
        )
        assert action.rights_issue_price is None

    def test_rights_issue_requires_price(self) -> None:
        with pytest.raises(ValidationError):
            CorporateAction(
                symbol="SZ000001",
                ex_date=date(2026, 5, 10),
                rights_issue_ratio=0.2,
            )

    def test_all_zero_components_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CorporateAction(symbol="SH600519", ex_date=date(2026, 6, 20))
