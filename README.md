# pulsar-contracts

The single public contract library of the **Pulsar** A-share quantitative trading
system: the two cross-repository ports (`MarketDataPort`, `ExecutionPort`) and
the immutable domain objects exchanged through them.

This repository is the root of Pulsar's multi-repo topology. Every other
repository (`pulsar-core`, `pulsar-data`, `pulsar-exec`, `pulsar-app`) depends
on this package; this package depends on nothing but basic libraries.

## Scope

- **Ports** — `typing.Protocol` definitions; implementations live in
  `pulsar-data` (data adapters) and `pulsar-exec` (venues/brokers/gateways).
- **Domain objects** — frozen pydantic models; no behavior, no I/O, no
  business logic.
- **Nothing else.** No adapters, no strategy code, no configuration handling.

Dependencies: `pydantic` and `pandas` only. Python >= 3.11.

## Installation

```bash
pip install .
# development
pip install -e .[dev]
```

## Ports

```python
from datetime import date
from typing import Callable

from pulsar_contracts import MarketDataPort, ExecutionPort


class MarketDataPort(Protocol):
    def list_instruments(self, as_of: date) -> list[Instrument]: ...
    def fetch_bars(self, symbols: list[str], start: date, end: date,
                   freq: Freq, adjust: AdjustMode) -> DataFrame: ...
    def fetch_corporate_actions(self, symbol: str) -> list[CorporateAction]: ...
    def calendar(self, start: date, end: date) -> list[date]: ...
    def subscribe(self, symbols: list[str],
                  on_snapshot: Callable[[Snapshot], None]) -> Subscription: ...


class ExecutionPort(Protocol):
    def submit(self, intent: OrderIntent) -> OrderId: ...          # idempotent
    def cancel(self, order_id: OrderId) -> CancelResult: ...
    def query(self, order_id: OrderId) -> OrderState: ...
    def positions(self) -> list[Position]: ...
    def on_event(self, callback: Callable[[ExecutionEvent], None]) -> None: ...
```

Semantic highlights (full definitions in the module docstrings):

- `fetch_*` / `calendar` / `list_instruments` read the local data lake —
  synchronous, deterministic, reproducible; missing in-range trading days
  raise instead of returning partial data.
- `subscribe` is best-effort for Paper/Live; late or missing snapshots are
  detected through `Snapshot.seq` gaps, never by exceptions.
- `submit` is idempotent: the same `IdempotencyKey` (`run_id` + `seq`)
  always maps to the same `OrderId`.
- `ExecutionEvent` kinds: `ACCEPTED`, `REJECTED`, `PARTIAL_FILL`, `FILL`,
  `CANCELLED`, `ERROR`.
- Shared order state machine:
  `CREATED → SUBMITTED → PARTIALLY_FILLED → FILLED`, with `REJECTED` and
  `CANCELLED` as the other terminal states.
- All timestamps are timezone-aware datetimes normalized to Asia/Shanghai;
  bar timestamps are left-closed (a daily bar carries `00:00` of its
  trading day).
- `fetch_bars` returns a DataFrame with the canonical columns
  `symbol, ts, open, high, low, close, volume, amount, adjust_factor, quality`.

## Domain objects

| Object             | Purpose                                                      |
| ------------------ | ------------------------------------------------------------ |
| `Instrument`       | Symbol, exchange, board, ST flag, listing state, share capital |
| `Bar`              | OHLCV + turnover + adjustment factor + quality mark          |
| `Snapshot`         | Realtime snapshot with best-five book levels and sequence no. |
| `CorporateAction`  | Cash dividend / bonus shares / rights issue per ex-date      |
| `OrderIntent`      | Channel-agnostic intent with idempotency key                 |
| `Order`            | Venue-confirmed order mirroring its intent                   |
| `Fill`             | Fill report with per-fill fee breakdown                      |
| `Position`         | Holdings with T+1 available quantity                         |
| `ExecutionEvent`   | Port-pushed lifecycle event                                  |

Supporting value types: `Freq`, `AdjustMode`, `DataQuality`, `Side`,
`PriceMode`, `TimeInForce`, `OrderStatus`, `IdempotencyKey`, `OrderId`,
`Subscription`, `Timestamp`.

## Development

```bash
pip install -e .[dev]
python -m mypy        # strict typecheck
python -m pytest      # unit tests + import purity checks
```

CI (`.github/workflows/ci.yml`) runs on every push/PR: strict typecheck,
import-purity checks, the test suite on Python 3.11–3.13, and a
non-editable `pip install` smoke check.

## Versioning

The package follows semantic versioning. Ports and domain-object schemas are
the system-wide public API: breaking changes require a major version bump and
coordinated updates of all downstream repositories.

## License

MIT — see [LICENSE](LICENSE).
