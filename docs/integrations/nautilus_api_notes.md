# NautilusTrader — pinned API notes

- Package: `nautilus_trader==1.231.0` (released 2026-08-02; `requires_python >=3.12,<3.15`)
- Upstream: [`nautechsystems/nautilus_trader` @ tag `v1.231.0`](https://github.com/nautechsystems/nautilus_trader/tree/v1.231.0), commit `27a8e54e7ac3c57d6cbf8891f0283dfbaee97317`
- Wheel: `nautilus_trader-1.231.0-cp314-cp314-win_amd64.whl` (115,322,233 bytes) exists on PyPI.
- Verified: 2026-10-10 by reading the v1.231.0 source tree and PyPI JSON. Unverifiable items are marked **UNVERIFIED** and must not be relied on in code.

## 0. Documentation warning (read this first)

**`https://nautilustrader.io/docs/latest/` documents v2.0.0rcN — the Rust-core rewrite — and
describes a different adapter interface** (`nautilus_trader.live.clients`, `LiveNodeBuilder`,
`_subscribe_quotes`, `_handle_report`). There is no versioned `1.231.0` docs path (`/docs/1.231.0/`
returns 404); only `latest` and `nightly` exist. Worse, `docs/developer_guide/adapters.md` *inside the
v1.231.0 tag* is already v2-oriented and is **not** a valid v1 guide.

Consequence for this project: the CR-04 adapter must be written against the **v1.231.0 source tree at
the pinned tag**, not against any published guide, and the compatibility test must assert the interface
shapes below so a future wheel bump fails loudly instead of silently drifting.

## 1. Base classes

| Role | Import |
|---|---|
| `DataClient` / `MarketDataClient` | `nautilus_trader.data.client` |
| `LiveDataClient` / `LiveMarketDataClient` | `nautilus_trader.live.data_client` |
| `ExecutionClient` | `nautilus_trader.execution.client` |
| `LiveExecutionClient` | `nautilus_trader.live.execution_client` |
| `InstrumentProvider` | `nautilus_trader.common.providers` |

Subclass the **Live** variants for a live/paper adapter; they own loop and task plumbing.

```python
DataClient.__init__(self, ClientId client_id, MessageBus msgbus, Cache cache, Clock clock,
                   Venue venue=None, config: NautilusConfig | None = None)
LiveDataClient.__init__(self, loop: asyncio.AbstractEventLoop, client_id: ClientId,
                        venue: Venue | None, msgbus: MessageBus, cache: Cache, clock: LiveClock,
                        config: NautilusConfig | None = None)
LiveMarketDataClient.__init__(..., instrument_provider: InstrumentProvider, config=None,
                              is_sync: bool = False)
ExecutionClient.__init__(self, ClientId client_id, Venue venue, OmsType oms_type,
                         AccountType account_type, Currency base_currency, MessageBus msgbus,
                         Cache cache, Clock clock, config: NautilusConfig | None = None)
LiveExecutionClient.__init__(self, loop, client_id, venue, oms_type, account_type, base_currency,
                             instrument_provider, msgbus, cache, clock: LiveClock, config=None)
```

Stability: these v1 names are the released 1.x public interface, but the whole v1 line is in
maintenance while v2.0.0rcN is the new line. `TradingNodeBuilder` internals and the `_py`/`cdef` split
are **not** stable.

## 2. Configs and factories

```python
from nautilus_trader.config import TradingNodeConfig, InstrumentProviderConfig
from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig, RoutingConfig

class LiveDataClientConfig(NautilusConfig, frozen=True):
    handle_revised_bars: bool = False
    instrument_provider: InstrumentProviderConfig = InstrumentProviderConfig()
    routing: RoutingConfig = RoutingConfig()

class LiveExecClientConfig(NautilusConfig, frozen=True):
    instrument_provider: InstrumentProviderConfig = InstrumentProviderConfig()
    routing: RoutingConfig = RoutingConfig()

class RoutingConfig(NautilusConfig, frozen=True):
    default: bool = False
    venues: frozenset[str] | None = None
```

Factories (`nautilus_trader.live.factories`), static, unchanged in 1.231.0:

```python
class LiveDataClientFactory:
    @staticmethod
    def create(loop: asyncio.AbstractEventLoop, name: str, config: LiveDataClientConfig,
               msgbus: MessageBus, cache: Cache, clock: LiveClock) -> LiveDataClient: ...

class LiveExecClientFactory:
    @staticmethod
    def create(loop: asyncio.AbstractEventLoop, name: str, config: LiveExecClientConfig,
               msgbus: MessageBus, cache: Cache, clock: LiveClock) -> LiveExecutionClient: ...
```

Registration: `TradingNode.add_data_client_factory(name, factory)` /
`add_exec_client_factory(name, factory)`; the `name` must match the key used in
`TradingNodeConfig.data_clients` / `.exec_clients`. Any rule beyond literal key matching is
**UNVERIFIED** — do not rely on prefix matching.

`TradingNode.__init__(config=None, loop=None)`, `.build()` (required before run),
`.run(raise_exception=False)`, `await .run_async()`, `.stop()`, `await .stop_async()`, `.dispose()`.

## 3. Methods to implement

Data side — the Live class splits **sync dispatchers** (do not override) from **async coroutines** (override):

- Override: `async _connect`, `async _disconnect`, `async _subscribe_instrument(s)`,
  `async _subscribe_quote_ticks(SubscribeQuoteTicks)`, `async _subscribe_bars(SubscribeBars)`,
  `async _subscribe_trade_ticks`, `async _unsubscribe_*`, `async _request_instrument(RequestInstrument)`,
  `async _request_bars(RequestBars)`, `async _request_instruments`, `async _request_quote_ticks`,
  `async _request_trade_ticks` — all `-> None`.
- Push data into the engine. Because `DataClient` is a `cdef` class, Python adapters must call the
  Python wrappers: `_handle_data_py(Data)`, `_handle_instrument_py(...)`,
  `_handle_quote_ticks_py(instrument_id, list ticks, UUID4 correlation_id, datetime start, datetime end, dict params=None)`,
  `_handle_bars_py(BarType, list bars, UUID4 correlation_id, datetime start, datetime end, dict params=None)`.

Execution side — override on `LiveExecutionClient`:
`async _connect`, `async _disconnect`, `async _submit_order(SubmitOrder)`,
`async _submit_order_list(SubmitOrderList)`, `async _modify_order(ModifyOrder)`,
`async _cancel_order(CancelOrder)`, `async _cancel_all_orders(CancelAllOrders)`,
`async _batch_cancel_orders(BatchCancelOrders)`.
Sync dispatchers (`submit_order`, `cancel_order`, `query_account`, `query_order`, …) are not overridden.

Reconciliation coroutines, exact:

```python
async def generate_order_status_report(self, command: GenerateOrderStatusReport) -> OrderStatusReport | None
async def generate_order_status_reports(self, command: GenerateOrderStatusReports) -> list[OrderStatusReport]
async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]
async def generate_position_status_reports(self, command: GeneratePositionStatusReports) -> list[PositionStatusReport]
async def generate_mass_status(self, lookback_mins: int | None = None) -> ExecutionMassStatus | None
```

## 4. Orders and value types

- Orders: `nautilus_trader.model.orders.market.MarketOrder`, `...orders.limit.LimitOrder`.
  `MarketOrder(..., time_in_force=TimeInForce.GTC, ...)` — `GTD` is rejected for market orders.
  `LimitOrder` adds `price` and `expire_time_ns` (required non-zero for GTD, zero otherwise).
- Naive UTC nanosecond timestamps are used throughout; the adapter is responsible for timezone
  normalisation.
- `nautilus_trader.model.objects`: `Quantity` (`from_str`, `from_int`, `from_decimal`, `zero`),
  `Price` (`from_str`, `from_int`, `from_decimal`), `Money`.
- Enums (from `nautilus_trader.core.rust.model`, re-exported by `nautilus_trader.model.enums`):
  `OrderSide.{NO_ORDER_SIDE, BUY, SELL}`, `TimeInForce.{GTC, IOC, FOK, GTD, DAY, AT_THE_OPEN,
  AT_THE_CLOSE}`, `PositionSide.{FLAT, LONG, SHORT}`, `LiquiditySide.{NO_LIQUIDITY_SIDE, MAKER, TAKER}`,
  `OmsType.UNSPECIFIED`. `OrderType` member names beyond `MARKET`/`LIMIT` and `AccountType` members are
  **UNVERIFIED**.
- Division of responsibility: the **engine** owns the order state machine (`Order._fsm`,
  `filled_qty`, position-id resolution). The **client** translates commands into venue calls and emits
  execution events (`generate_order_accepted/submitted/rejected/...`) or reconciliation reports.
  There is no `OrderMatchingEngine` in v1; in-process simulation is the `OrderEmulator`
  (`nautilus_trader.execution.emulator`) driven by `OrderEmulatorConfig` — direct adapter use is
  **UNVERIFIED**.

## 5. Reports and account state

```python
from nautilus_trader.execution.reports import (OrderStatusReport, FillReport,
                                               PositionStatusReport, ExecutionMassStatus)

OrderStatusReport(account_id, instrument_id, venue_order_id, order_side, order_type, time_in_force,
                  order_status, quantity, filled_qty, report_id, ts_accepted, ts_last, ts_init,
                  client_order_id=None, order_list_id=None, venue_position_id=None,
                  linked_order_ids=None, parent_order_id=None,
                  contingency_type=ContingencyType.NO_CONTINGENCY, expire_time=None, price=None,
                  trigger_price=None, trigger_type=TriggerType.NO_TRIGGER, limit_offset=None,
                  trailing_offset=None, trailing_offset_type=None, avg_px=None, display_qty=None,
                  post_only=False, reduce_only=False, cancel_reason=None, ts_triggered=None)

FillReport(account_id, instrument_id, venue_order_id, trade_id, order_side, last_qty, last_px,
           commission, liquidity_side, report_id, ts_event, ts_init, avg_px=None,
           client_order_id=None, venue_position_id=None)

PositionStatusReport(account_id, instrument_id, position_side, quantity, report_id, ts_last,
                     ts_init, venue_position_id=None, avg_px_open=None)
#   helper: PositionStatusReport.create_flat(account_id, instrument_id, size_precision, ts_init,
#                                            report_id=None)

ExecutionMassStatus(client_id, account_id, venue, report_id, ts_init)
#   .add_order_reports(list), .add_fill_reports(list), .add_position_reports(list)
```

Account state: `ExecutionClient.generate_account_state(balances, margins, reported, ts_event, info=None)`
builds `AccountState(account_id, account_type, base_currency, reported, balances, margins, info,
event_id, ts_event, ts_init)`. The field is **`reported`**, not `is_reported`, in 1.231.0.
`AccountBalance(total, locked, free)`; `MarginBalance(initial, maintenance, instrument_id=None)`.

`LiveExecutionClient` logs that aborted cancel tasks may leave orders open at the venue ("increase
`timeout_post_stop`") — i.e. uncertain outcomes are expected and must be reconciled, which is exactly
ADR-0008 §3.

## 6. US equities and ETFs

```python
from nautilus_trader.model.instruments import Equity   # file: model/instruments/equity.pyx

Equity(instrument_id, raw_symbol, currency, price_precision, price_increment: Price, lot_size: Quantity,
       ts_event, ts_init, max_quantity=None, min_quantity=None, margin_init=None, margin_maint=None,
       maker_fee=None, taker_fee=None, isin=None, tick_scheme_name=None, info=None)
```

`Equity` hard-codes `asset_class=EQUITY`, `instrument_class=SPOT`, `size_precision=0`,
`size_increment=1`, `multiplier=1`, `is_inverse=False`. (Its docstring says `price_increment: Decimal`;
the source takes a `Price` — trust the source.)

- Venues: `Venue("XNAS")`; `InstrumentId`, `ClientOrderId`, `AccountId`, `TraderId`, `VenueOrderId`,
  `TradeId`, `PositionId`, `Symbol` all come from `nautilus_trader.model.identifiers`. Built-in
  `XNAS`/`XNYS`/`ARCA` constants are **UNVERIFIED** — construct the venue string ourselves. Note
  "ARCA" is not an ISO-10383 MIC ("ARCX" is).
- `Currency`: **`Currency.USD` as a class attribute is UNVERIFIED — do not rely on it.** `Currency.from_str`
  resolves through the Rust currency map and falls back to a **CRYPTO** currency with precision 8 for
  unknown codes. The CR-04 compatibility test must resolve USD explicitly and assert its precision
  rather than assume.
- `InstrumentProvider` (`nautilus_trader.common.providers`) holds adapter-local instruments;
  `load_all_async()` / `load_ids_async(...)`, configured via
  `InstrumentProviderConfig(load_all=True)` or `load_ids=[...]` on `LiveDataClientConfig.instrument_provider`.

## 7. Threading and async rules

- Async (must be coroutines on the node's single asyncio loop): all `_connect`/`_disconnect`,
  `_subscribe_*`/`_unsubscribe_*`, `_request_*`, `_submit_*`, `_modify_order`, `_cancel_*`,
  `generate_*` report coroutines.
- `self._loop` is that loop. Background work goes through
  `self.create_task(coro, log_msg=None, actions=None, success_msg=None, success_color=LogColor.NORMAL)`,
  which registers the task in `self._tasks` and attaches completion logging.
- Shutdown: `disconnect()` schedules `_disconnect()` then `cancel_pending_tasks(timeout_secs=5.0)`
  (`nautilus_trader.live.cancellation.cancel_tasks_with_timeout`).
- **Blocking rule:** the official v1 documentation does not state one (the prose found is from the v2
  guide) — treat "documented blocking rule" as **UNVERIFIED**. The engineering conclusion is
  unavoidable though: every hook runs on the shared node loop, so synchronous HTTP (`tigeropen` is
  synchronous) must be moved off the loop with `asyncio.to_thread`/`run_in_executor` or it will stall
  the data and execution engines.

## 8. There is no PAPER environment

`Environment` is `{BACKTEST, SANDBOX, LIVE}` (`from nautilus_trader.common import Environment`).
NautilusTrader therefore **cannot express our PAPER mode directly**:

| TigerQuant mode | NautilusTrader environment | Broker writes |
|---|---|---|
| BACKTEST | `BACKTEST` | none |
| SHADOW | `LIVE` data only, no exec client | none |
| PAPER (Tiger paper account) | `LIVE` + Tiger adapter guarded to the verified paper account | only to the Tiger paper account |
| LIVE | not built, not authorized | — |

The dangerous consequence: **paper and live are the same engine environment**, distinguished only by our
account-identity guard (ADR-0003, `docs/integrations/tigeropen_api_notes.md` §8). The CR-04 work must
therefore expose the guard as a construction-time precondition and must never let
`Environment.SANDBOX` (which simulates fills locally) be confused with a real Tiger paper account —
those are different things and are labelled differently (`SIMULATED` vs `PAPER_VERIFIED`).

Minimal node shape (verified field names):

```python
config = TradingNodeConfig(
    environment=Environment.SANDBOX,          # or Environment.LIVE for the guarded paper account
    trader_id="TRADER-001",
    data_clients={"TIGERQ-DATA": TigerDataConfig(instrument_provider=InstrumentProviderConfig(load_all=True))},
    exec_clients={"TIGERQ-EXEC": TigerExecConfig()},
    strategies=[ImportableStrategyConfig(strategy_path="pkg.strat:MyStrategy",
                                         config_path="pkg.strat:MyConfig", config={})],
)
node = TradingNode(config=config)
node.add_data_client_factory("TIGERQ-DATA", TigerDataClientFactory)
node.add_exec_client_factory("TIGERQ-EXEC", TigerExecutionClientFactory)
node.build()
```

`TradingNode` has no `add_strategy`/`add_venue` in 1.231.0. `ImportableStrategyConfig` field names
beyond `strategy_path`/`config_path`/`config` are **UNVERIFIED**.

## 9. Durability — and why we still keep our own journal

Available: process-local `Cache` (+ optional Redis backing via `CacheConfig(database=...)`), message-bus
persistence via `MessageBusConfig(database=...)`, `ParquetDataCatalog` via
`catalogs=[DataCatalogConfig(...)]` and `StreamingConfig`, strategy state via
`NautilusKernelConfig.load_state`/`save_state` (both default `False`). `TradingNode` itself has no
`save()`/`load()`.

**Attribution correction:** no official v1 statement was found that these are "not a substitute for an
independent audit store". That is *our* engineering conclusion, adopted in `docs/ARCHITECTURE.md` and
ADR-0004: the cache is process-local, Redis/Parquet backing is best-effort state persistence,
`save_state` covers strategy state only, and none of it is an append-only, independently verifiable
trade audit log. It must not be cited as a Nautilus-documented caveat.

## 10. Wheel and dependency notes

`1.231.0` hard dependencies that must resolve on cp314/Windows: `msgspec>=0.21.1,<1.0.0`,
`pyarrow>=25.0.0`, `pandas>=2.3.3,<4.0.0`, `numpy>=1.26.4`, `fsspec==2026.2.0`,
`click>=8.4.1,<9.0.0`, `tqdm>=4.68.4,<5.0.0`, `pytz>=2026.2`, `tzdata>=2026.3`, `portion>=2.6.1`.
`uvloop==0.22.1` is `sys_platform != "win32"` and is not installed on Windows.
