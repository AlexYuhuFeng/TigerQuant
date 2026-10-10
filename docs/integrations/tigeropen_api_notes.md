# Tiger OpenAPI SDK — pinned API notes

- Package: `tigeropen==3.8.0` (PyPI latest; pure-Python wheel `tigeropen-3.8.0-py3-none-any.whl`)
- Upstream: [`tigerfintech/openapi-python-sdk` @ tag `v3.8.0`](https://github.com/tigerfintech/openapi-python-sdk/tree/v3.8.0), commit `605e7929a1af435dee75de5738d070372ad47153`
- Verified: 2026-10-10, by reading the v3.8.0 source tree and the official Tiger docs. Anything not verifiable is marked **UNVERIFIED** and must not be relied on in code.
- Consumer: CR-02 (read-only) and CR-03 (paper execution). This file is the contract `docs/adr/ADR-0010` refers to.

## 1. Imports that actually exist in 3.8.0

| Purpose | Import |
|---|---|
| Trade client | `from tigeropen.trade.trade_client import TradeClient` |
| Quote client | `from tigeropen.quote.quote_client import QuoteClient` |
| Push client | `from tigeropen.push.push_client import PushClient` |
| Config factory | `from tigeropen.tiger_open_config import TigerOpenClientConfig, get_client_config` |
| Constants/enums | `from tigeropen.common.consts import Market, SecurityType, Currency, OrderStatus, OrderType, OrderType as OT` |
| Order domain | `from tigeropen.trade.domain.order import Order, Transaction` |
| Accounts | `from tigeropen.trade.domain.account import Account, PortfolioAccount, MarketValue` |
| Account profile | `from tigeropen.trade.domain.profile import AccountProfile` |
| Paper detection | `from tigeropen.common.util.account_util import AccountUtil` |
| Contract helper | `from tigeropen.common.util.contract_utils import stock_contract` |

**Does not exist** in 3.8.0: `tigeropen.common.util.tiger_config`, `TigerHttpClient`, `TigerClientConfig`, a `SecType` enum, an `Action` enum, a `TimeInForce` enum. `action` is the plain string `'BUY'`/`'SELL'`; `time_in_force` is `'DAY'`/`'GTC'`.

## 2. Client construction and endpoints

```python
TradeClient(client_config, logger=None)          # tigeropen.trade.trade_client
TigerOpenClientConfig(sandbox_debug=None, enable_dynamic_domain=True, props_path=None)
get_client_config(private_key_path=None, tiger_id=None, account=None, sandbox_debug=False, ...)
```

Setting `config.account` to an all-digit value of length ≥ 17 flips `config.is_paper = True`
(`AccountUtil.is_paper_account`, `PAPER_ACCOUNT_DIGIT_LEN = 17`).

| Environment | Host |
|---|---|
| default / live (global) | `openapi.tigerfintech.com` → `https://openapi.tigerfintech.com/gateway`, socket port 9883 |
| live US (`License.TBUS`) | `openapi.tradeup.com` → `https://openapi.tradeup.com/gateway`, port 9983 |
| **sandbox / paper** | `openapi-sandbox.tigerfintech.com` → `https://openapi-sandbox.tigerfintech.com/gateway`, socket 9885 |

With `enable_dynamic_domain=True` (the default), `refresh_server_info()` selects
`f"{license}-PAPER"` when `is_paper` is true. `sandbox_debug=True` combined with
`enable_dynamic_domain=True` raises `NotImplementedError` — it is deprecated and must not be
used to force sandbox.

Tiger does not publish host lists: they are embedded in the SDK
([FAQ configuration](https://quant.itigerup.com/openapi/zh/python/FAQ/configuration.html)).

## 3. Account discovery and paper verification

```python
TradeClient.get_managed_accounts(account=None, lang=None) -> ProfilesResponse
#   .profiles -> list[AccountProfile(account, capability, status, account_type=None)]
```

- `capability` ∈ `CASH | MGRN | PMGRN`; `status` ∈ `New, Funded, Open, Pending, Abandoned, Rejected, Closed, Unknown`.
- Tiger exposes paper errors as a separate server error family: **code 1300 "paper account response error"** (模拟账号交易错误), vs `1100` global and `1200` prime account errors ([error handling](https://quant.itigerup.com/openapi/zh/python/errorHandle/overview.html)).
- The official FAQ states the `account` field holds the live **or** paper account you intend to use; there is no separate mode flag in the request path.

**The literal value of `account_type` for a paper account is UNVERIFIED.** The only verified paper
predicate is `AccountUtil.is_paper_account(account)` (all digits, length ≥ 17). The adapter must
therefore treat "unknown account type" as NO_TRADE rather than assuming, and must record whatever
`account_type`/`capability`/`status` the API actually returns.

Consequence for `config/example.yaml`: `broker.require_reported_account_type: PAPER` cannot be
satisfied by a documented field in 3.8.0. The adapter implements it as a **three-part assertion**
(see §8) and records the raw response, rather than string-comparing an undocumented field.

## 4. Read-only calls (v3.8.0 signatures)

| Call | Returns |
|---|---|
| `get_assets(account=None, sub_accounts=None, segment=False, market_value=False)` | `response.assets: list[PortfolioAccount]`; `.summary: Account`, `.segments`, `.market_value(currency): MarketValue` |
| `get_positions(account=None, sec_type=SecurityType.STK, currency=Currency.ALL, market=Market.ALL, symbol=None, ...)` | `list[Position]` |
| `get_orders(account=None, sec_type=None, market=Market.ALL, symbol=None, start_time=None, end_time=None, limit=100, is_brief=False, states=None, sort_by=None, seg_type=None, lang=None, page_token=None)` | `list[Order]` (or `OrdersResponse` when `page_token` given) |
| `get_open_orders(account=None, ...)` | `response.result` |
| `get_order(account=None, id=None, order_id=None, is_brief=False, show_charges=None, lang=None)` | `Order | None` |
| `get_transactions(...)` | prime accounts only |

`Account` fields: `net_liquidation, accrued_cash, accrued_dividend, available_funds, buying_power, cash,
currency, cushion, day_trades_remaining, equity_with_loan, excess_liquidity, gross_position_value,
initial_margin_requirement, maintenance_margin_requirement, realized_pnl, regt_equity, regt_margin, sma,
timestamp, unrealized_pnl`.

`Position` fields: `account, contract, quantity, average_cost, market_price, market_value, realized_pnl,
unrealized_pnl, position_scale, unrealized_pnl_percent, position_qty, salable_qty, today_pnl,
last_close_price, ...`. **There is no `symbol`/`latest_price` on `Position`** — use
`position.contract.symbol` and `position.market_price`.

`Order` fields include: `account, id, order_id, parent_id, order_time, trade_time, contract, action,
quantity, filled, remaining (property), avg_fill_price, commission, realized_pnl, status, limit_price,
aux_price, order_type, time_in_force, outside_rth, external_id, is_open, latest_price, sub_ids, orders`.

`get_order` returns `response.result[0]` only when exactly one result, else `None`; a non-success
status raises `ApiException`.

## 5. Write calls

```python
Order(account, contract, action, order_type=None, quantity=None, limit_price=None,
      aux_price=None, trail_stop_price=None, trailing_percent=None, percent_offset=None,
      time_in_force=None, outside_rth=None, ..., order_id=None, external_id=..., **kwargs)

TradeClient.create_order(account, contract, action, order_type, quantity, limit_price=None, ...)
TradeClient.preview_order(order, lang=None) -> dict
TradeClient.place_order(order: Order, lang=None) -> int | None   # returns the global order id
TradeClient.modify_order(order, quantity=None, limit_price=None, ..., lang=None) -> int | None
TradeClient.cancel_order(account=None, id=None, order_id=None) -> int | None
```

`place_order` sends `params.account = order.account` — **not** `config.account`.
`OrderType` ∈ `MKT, LMT, STP, STP_LMT, TRAIL, AM, AL, TWAP, VWAP, OCA, ICEBERG`.
`OrderStatus` values: `PendingNew, Initial, Submitted, PartiallyFilled, Filled, Cancelled,
PendingCancel, Inactive, Invalid` (mapped from `PENDING_NEW`, `NEW`, `HELD`, `PARTIALLY_FILLED`,
`FILLED`, `CANCELLED`, `PENDING_CANCEL`, `REJECTED`, `EXPIRED`).

## 6. Errors, limits and ambiguous outcomes

- Documented codes: `2` network read timeout, `5` rate limit (HTTP 429, limit stated in the message),
  `4` access forbidden, `1000/1010` parameter errors, `1100` global account error, `1200` prime account
  error, `1300` paper account error, `2100/2200/2300` quote errors, `3xxx` subscription errors,
  `4000` permission, `4001` kick-out.
- **The SDK never auto-retries order mutations**: `SKIP_RETRY_SERVICES = {PLACE_ORDER, CANCEL_ORDER,
  MODIFY_ORDER}`. Backoff retry (`retry_max_tries=5`, `retry_max_time=60`) applies only to read services.
  An ambiguous timeout therefore propagates as an exception, which is exactly the
  `SUBMIT_UNKNOWN` case in ADR-0008.
- **There is no broker-side idempotency key.** `PlaceModifyOrderParams` has no client-order-id field.
  `Order.external_id` exists as a passthrough and is echoed by `get_order`, but its idempotency
  semantics are **UNVERIFIED**. Consequences: (a) a stable local `client_order_id` is recorded for our
  own correlation only; (b) duplicate-order prevention rests entirely on our own reconciliation and
  quarantine, never on the broker; (c) we must never resubmit after an ambiguous outcome.
- Numeric rate limits and per-order size limits are **not published (UNVERIFIED)**; the adapter must
  treat code `5` as a hard backoff-and-stop signal rather than guessing a budget.

## 7. Push (streaming)

`PushClient(host, port, use_ssl=True, connection_timeout=30, heartbeats=(10000, 10000),
use_protobuf=True, client_config=None, callback_executor=None)`, `connect(tiger_id=None, private_key=None)`.
Subscriptions: `subscribe_order`, `subscribe_asset`, `subscribe_position`, `subscribe_transaction`,
`subscribe_quote`, `subscribe_kline`, plus `unsubscribe_*`. Callbacks: `order_changed`, `asset_changed`,
`position_changed`, `transaction_changed`, `quote_changed`, `connect_callback`, `disconnect_callback`,
`error_callback`, `kickout_callback`, `heartbeat_callback`.

Proto payloads: `OrderStatusData(id, account, symbol, action, market, currency, secType, orderType,
totalQuantity, filledQuantity, avgFillPrice, limitPrice, stopPrice, realizedPnl, status, replaceStatus,
cancelStatus, outsideRth, canModify, canCancel, errorMsg, commissionAndFee, openTime, updateTime, ...)`;
`AssetData(account, currency, segType, availableFunds, excessLiquidity, netLiquidation, equityWithLoan,
buyingPower, cashBalance, grossPositionValue, initMarginReq, maintMarginReq, timestamp)`.

**Duplicate push delivery is not documented.** Treat the stream as at-least-once and dedupe
(acceptance gate 7). Exact Python argument tuples for the callbacks are **UNVERIFIED**.
Tiger warns that sandbox tiger ids are shared, sandbox data may be missing, and `4001 kick out by a new
connection` means one long connection per tiger id — do not auto-reconnect in a loop
([FAQ push](https://quant.itigerup.com/openapi/zh/python/FAQ/push.html)).

## 8. Paper limitations and the preconditions a safe adapter must assert

Official limitations ([FAQ trading](https://quant.itigerup.com/openapi/zh/python/FAQ/trade.html)): paper
accounts do not support pre-market/after-hours or pre-placement trading in some markets/products.
Order-type support is per market, not paper-specific. Whether Tiger itself simulates paper fills is
**not documented (UNVERIFIED)**, so paper fill behaviour must be measured, not assumed.

Verified hazards that make a naive "paper" configuration unsafe:

1. Nothing validates that `config.account` is a paper account, nor that `is_paper` agrees with it.
2. An explicitly set `server_url` is never overridden — a misconfigured process can post to a live gateway.
3. The `account` setter only ever sets `is_paper = True`, never `False`; switching to a live account
   number afterwards leaves `is_paper` stale-true. `is_paper` is also freely writable.
4. `place_order`/`modify_order` use `order.account`, not `config.account`; an `Order` built with a live
   account is submittable through a paper-configured client.
5. The quote host lookup ignores `is_paper`, so quotes are not paper-scoped.
6. `sandbox_debug` is deprecated and raises under the default dynamic-domain configuration.

Mandatory preconditions before any write (implemented in `adapters/tiger/guard.py`, CR-02/CR-03):

1. `get_managed_accounts()` must contain the target account; record `capability`, `status`, `account_type`
   verbatim in the audit journal.
2. `AccountUtil.is_paper_account(config.account) is True` **and** `config.is_paper is True`; an unknown or
   missing account type is treated as NO_TRADE, not as PASS.
3. For every `place_order`/`modify_order`/`cancel_order`: `order.account == config.account == verified
   paper account`. Any mismatch refuses locally and journals a CRITICAL alert.
4. The resolved host must be the sandbox host; refuse to construct the client when `server_url` resolves
   to `openapi.tigerfintech.com` or `openapi.tradeup.com`.
5. Never blind-retry a mutation. Reconcile with `get_order`/`get_orders` by `order_id`/`id` first; if the
   outcome stays unknown, quarantine (ADR-0008 §3).
6. Treat `external_id` as a correlation hint only; it carries no verified idempotency guarantee.

## 9. Dependencies

`requires_python >= 3.8`; runtime deps: `simplejson, delorean, pandas, python-dateutil, pytz, pyasn1,
rsa, stomp.py, getmac, cryptography, backoff, jproperties, protobuf, urllib3, click>=8.0`. Pure Python
wheel (see `docs/evidence/cr00_pypi_probe.json`).

## 10. Approval gate

Per `docs/WORK_QUEUE.md`, CR-02 and CR-03 require an actual Tiger paper account. No credentials are
available to this project, so those CRs remain **BLOCKED** for integration evidence. Everything above is
used to build the adapter and its fake-SDK contract tests, which stay labelled `SIMULATED` until a real
paper connection is recorded.
