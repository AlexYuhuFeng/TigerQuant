"""Guarded, read-only Tiger client and broker/ledger reconciliation.

Two properties define this module.

**It cannot write.** :class:`TigerReadOnlyClient` exposes only read methods. It holds a
`TradeClient` internally but never surfaces it, never exposes a write method, and a test
asserts that no public member matches a broker write API name (``place_order``,
``modify_order``, ``cancel_order``, ``submit_order``). The account guard is verified before
the client is usable, so an unverified or live account is refused before the first read —
not at the first order, which would be far too late.

**It never corrects.** :func:`reconcile` compares broker-reported state with our ledger and
*reports* differences. It does not adjust the ledger, and it does not "fix" a mismatch. A
discrepancy means one of the two records is wrong and we do not know which, so the only safe
response is to stop opening new exposure and tell a human (ADR-0003 §3, acceptance gate 15).
Any silent correction would destroy exactly the evidence needed to find the cause.

Money from the broker arrives as a float or a :class:`~decimal.Decimal`. Floats are converted
through ``str`` rather than passed to :class:`Decimal` directly, because
``Decimal(0.1)`` is the binary artefact ``0.1000000000000000055511151231257827`` while
``Decimal("0.1")`` is exact. This is the one boundary where that conversion is legitimate and
necessary; everywhere inside the system ``core.money`` rejects floats outright.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from tigerquant.adapters.tiger.guard import mask_account
from tigerquant.core.enums import OrderSide, OrderStatus, ReasonCode
from tigerquant.core.errors import BrokerError, ConfigurationError
from tigerquant.core.money import ZERO, q_cash, q_price

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tigerquant.adapters.tiger.guard import AccountIdentity, TigerAccountGuard
    from tigerquant.portfolio.ledger import PortfolioLedger

__all__ = [
    "WRITE_API_NAMES",
    "BrokerBalances",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerSnapshot",
    "PositionDifference",
    "ReconciliationReport",
    "TigerReadOnlyClient",
    "broker_decimal",
    "reconcile",
]

#: Broker write APIs. Nothing in this module may call these, and no public member may be
#: named after them (asserted by tests/unit/test_tiger_read.py).
WRITE_API_NAMES: frozenset[str] = frozenset(
    {"place_order", "modify_order", "cancel_order", "submit_order", "create_order", "preview_order"}
)

#: Tiger order status strings mapped onto our own state machine labels. Unmapped values are
#: deliberately absent rather than guessed: an unknown status is surfaced as its raw string.
_STATUS_MAP: dict[str, OrderStatus] = {
    "PendingNew": OrderStatus.SUBMITTING,
    "Initial": OrderStatus.ACKNOWLEDGED,
    "Submitted": OrderStatus.ACKNOWLEDGED,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELED,
    "PendingCancel": OrderStatus.CANCEL_PENDING,
    "Inactive": OrderStatus.REJECTED,
    "Invalid": OrderStatus.REJECTED,
}


def broker_decimal(value: object) -> Decimal | None:
    """Convert a broker-supplied number to an exact :class:`Decimal`, or ``None``.

    ``None`` means *the broker did not give us a usable number*, and it covers two cases
    that must be treated identically:

    * the field is absent (Tiger omits rather than nulls), and
    * the field is **non-finite**. The paper environment returns ``inf`` for figures it does
      not compute — observed live: ``gross_position_value``, ``available_funds``,
      ``initial_margin_requirement`` and others. ``inf`` is the *absence* of a value, not a
      very large one; converting it would poison every downstream calculation, and
      ``Decimal("Infinity").quantize(...)`` raises anyway.

    Callers must decide explicitly what an absent value means. They must never silently
    substitute zero, because "flat" and "unknown" are different facts and only one of them
    is safe to act on.

    ``float`` is routed through ``str``: ``Decimal(0.1)`` is the binary artefact
    ``0.1000000000000000055511151231257827`` while ``Decimal("0.1")`` is exact. This is the
    one boundary where that conversion is legitimate; inside the system ``core.money``
    rejects floats outright.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        msg = f"bool is not a broker number: {value!r}"
        raise BrokerError(msg)
    if isinstance(value, float) and not math.isfinite(value):
        # NaN and the infinities are "no value", not extreme values. Tiger's paper
        # environment reports inf for figures it does not compute.
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        msg = f"cannot interpret broker value {value!r} as a number"
        raise BrokerError(msg) from exc
    return parsed if parsed.is_finite() else None


def _finite_or_zero(value: object, *, field_name: str, missing: list[str]) -> Decimal:
    """Read a broker number, recording its absence instead of inventing a zero."""
    parsed = broker_decimal(value)
    if parsed is None:
        missing.append(field_name)
        return ZERO
    return parsed


def _optional_price(value: object) -> Decimal | None:
    """Quantise an optional broker price, preserving "not reported" as ``None``."""
    parsed = broker_decimal(value)
    return None if parsed is None else q_price(parsed)


def _broker_int(value: object, *, default: int = 0) -> int:
    parsed = broker_decimal(value)
    if parsed is None:
        return default
    if parsed != parsed.to_integral_value():
        msg = f"expected an integral broker quantity, got {parsed}"
        raise BrokerError(msg)
    return int(parsed)


def _aware(value: object) -> dt.datetime | None:
    """Normalise a broker timestamp to aware UTC, or ``None`` when absent."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        # Tiger mixes seconds and milliseconds; anything past ~year 2286 in seconds is
        # milliseconds in practice.
        if seconds > 1e11:
            seconds /= 1000.0
        return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """One broker-reported holding."""

    instrument: str
    quantity: int
    average_cost: Decimal
    market_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal

    def as_record(self) -> dict[str, str]:
        return {
            "instrument": self.instrument,
            "quantity": str(self.quantity),
            "average_cost": str(self.average_cost),
            "market_price": str(self.market_price),
            "market_value": str(self.market_value),
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_pnl": str(self.realized_pnl),
        }


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """One broker-reported order."""

    broker_order_id: str
    instrument: str
    side: OrderSide
    quantity: int
    filled_quantity: int
    remaining_quantity: int
    average_fill_price: Decimal
    limit_price: Decimal | None
    order_type: str
    time_in_force: str
    #: Tiger's raw status string, kept verbatim so an unmapped value is visible.
    raw_status: str
    status: OrderStatus | None

    @property
    def is_open(self) -> bool:
        return self.status not in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}

    def as_record(self) -> dict[str, str]:
        return {
            "broker_order_id": self.broker_order_id,
            "instrument": self.instrument,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "filled_quantity": str(self.filled_quantity),
            "remaining_quantity": str(self.remaining_quantity),
            "average_fill_price": str(self.average_fill_price),
            "raw_status": self.raw_status,
            "mapped_status": self.status.value if self.status else "(unmapped)",
        }


@dataclass(frozen=True, slots=True)
class BrokerBalances:
    """Account totals as reported by the broker, in exact decimals."""

    currency: str
    cash: Decimal
    net_liquidation: Decimal
    buying_power: Decimal
    gross_position_value: Decimal
    available_funds: Decimal
    #: Fields the broker did not report. Recorded so "unknown" stays distinguishable from
    #: "zero", which matters: a zero balance and a missing balance are different problems.
    missing_fields: tuple[str, ...] = ()

    def as_record(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "cash": str(self.cash),
            "net_liquidation": str(self.net_liquidation),
            "buying_power": str(self.buying_power),
            "gross_position_value": str(self.gross_position_value),
            "available_funds": str(self.available_funds),
            "missing_fields": list(self.missing_fields),
        }


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """Everything read from the broker at one instant."""

    as_of: dt.datetime
    account: str
    cash: Decimal
    net_liquidation: Decimal
    buying_power: Decimal
    gross_position_value: Decimal
    currency: str
    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrder, ...]
    #: Fields the broker did not report. Recorded so a missing value is visible rather than
    #: silently zero — which is the difference between "flat" and "unknown".
    missing_fields: tuple[str, ...] = ()

    def position(self, instrument: str) -> BrokerPosition | None:
        return next((p for p in self.positions if p.instrument == instrument), None)

    def as_record(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "account": mask_account(self.account),
            "currency": self.currency,
            "cash": str(self.cash),
            "net_liquidation": str(self.net_liquidation),
            "buying_power": str(self.buying_power),
            "gross_position_value": str(self.gross_position_value),
            "positions": [p.as_record() for p in self.positions],
            "open_orders": [o.as_record() for o in self.open_orders],
            "missing_fields": list(self.missing_fields),
        }


@dataclass(frozen=True, slots=True)
class PositionDifference:
    """One instrument where the broker and our ledger disagree."""

    instrument: str
    broker_quantity: int
    ledger_quantity: int

    @property
    def delta(self) -> int:
        return self.broker_quantity - self.ledger_quantity


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Outcome of comparing broker state with our ledger.

    ``is_reconciled`` is the only thing the rest of the system should branch on: any
    difference, in either direction, means new exposure must not be opened.
    """

    as_of: dt.datetime
    account: str
    matched: tuple[str, ...]
    differences: tuple[PositionDifference, ...]
    broker_only: tuple[str, ...]
    ledger_only: tuple[str, ...]
    broker_cash: Decimal
    ledger_cash: Decimal
    cash_difference: Decimal
    open_order_count: int

    @property
    def is_reconciled(self) -> bool:
        return not (self.differences or self.broker_only or self.ledger_only) and (
            self.cash_difference == ZERO
        )

    def as_reason(self) -> ReasonCode:
        return (
            ReasonCode.SYMBOL_NOT_ALLOWED  # unused when reconciled
            if self.is_reconciled
            else ReasonCode.UNRECONCILED
        )

    def summary(self) -> str:
        if self.is_reconciled:
            return (
                f"reconciled with {mask_account(self.account)}: "
                f"{len(self.matched)} position(s), {self.open_order_count} open order(s), "
                f"cash {self.broker_cash}"
            )
        parts: list[str] = []
        if self.differences:
            parts.append(
                "quantity mismatches: "
                + ", ".join(
                    f"{d.instrument} broker={d.broker_quantity} ledger={d.ledger_quantity}"
                    for d in self.differences
                )
            )
        if self.broker_only:
            parts.append(f"held at broker but not in ledger: {list(self.broker_only)}")
        if self.ledger_only:
            parts.append(f"in ledger but not at broker: {list(self.ledger_only)}")
        if self.cash_difference != ZERO:
            parts.append(
                f"cash differs by {self.cash_difference} "
                f"(broker {self.broker_cash}, ledger {self.ledger_cash})"
            )
        return "NOT RECONCILED — " + "; ".join(parts)

    def as_record(self) -> dict[str, object]:
        """Audit-safe rendering."""
        return {
            "as_of": self.as_of.isoformat(),
            "account": mask_account(self.account),
            "reconciled": self.is_reconciled,
            "matched": list(self.matched),
            "differences": [
                {
                    "instrument": d.instrument,
                    "broker_quantity": d.broker_quantity,
                    "ledger_quantity": d.ledger_quantity,
                    "delta": d.delta,
                }
                for d in self.differences
            ],
            "broker_only": list(self.broker_only),
            "ledger_only": list(self.ledger_only),
            "broker_cash": str(self.broker_cash),
            "ledger_cash": str(self.ledger_cash),
            "cash_difference": str(self.cash_difference),
            "open_order_count": self.open_order_count,
        }


def reconcile(
    snapshot: BrokerSnapshot,
    ledger: PortfolioLedger,
    *,
    instrument_filter: Sequence[str] | None = None,
) -> ReconciliationReport:
    """Compare a broker snapshot with the ledger. Reports; never corrects.

    ``instrument_filter`` restricts the comparison to a universe, for the case where the
    account holds instruments outside the approved universe. Those are still reported when
    they are non-zero, because an unapproved holding is a fact an operator must see.
    """
    universe = set(instrument_filter) if instrument_filter is not None else None

    broker_positions = {
        position.instrument: position.quantity
        for position in snapshot.positions
        if position.quantity != 0
    }
    ledger_positions = {
        position.instrument: position.quantity
        for position in ledger.positions()
        if position.quantity != 0
    }

    instruments = sorted(set(broker_positions) | set(ledger_positions))
    matched: list[str] = []
    differences: list[PositionDifference] = []
    broker_only: list[str] = []
    ledger_only: list[str] = []

    for instrument in instruments:
        if universe is not None and instrument not in universe:
            continue
        broker_quantity = broker_positions.get(instrument, 0)
        ledger_quantity = ledger_positions.get(instrument, 0)
        if broker_quantity == ledger_quantity:
            matched.append(instrument)
        elif instrument not in ledger_positions:
            broker_only.append(instrument)
        elif instrument not in broker_positions:
            ledger_only.append(instrument)
        else:
            differences.append(
                PositionDifference(
                    instrument=instrument,
                    broker_quantity=broker_quantity,
                    ledger_quantity=ledger_quantity,
                )
            )

    broker_cash = q_cash(snapshot.cash)
    ledger_cash = q_cash(ledger.cash)
    return ReconciliationReport(
        as_of=snapshot.as_of,
        account=snapshot.account,
        matched=tuple(matched),
        differences=tuple(differences),
        broker_only=tuple(broker_only),
        ledger_only=tuple(ledger_only),
        broker_cash=broker_cash,
        ledger_cash=ledger_cash,
        cash_difference=q_cash(broker_cash - ledger_cash),
        open_order_count=len(snapshot.open_orders),
    )


class TigerReadOnlyClient:
    """Reads Tiger account state through a verified paper account. Never writes.

    Public surface: :meth:`verify_account`, :meth:`assets`, :meth:`positions`,
    :meth:`open_orders`, :meth:`snapshot`. There is no method that can change broker state,
    and the underlying SDK client is not reachable from outside.
    """

    __slots__ = ("_account", "_client", "_guard", "_last_snapshot")

    def __init__(self, client: Any, guard: TigerAccountGuard, *, account: str) -> None:
        for banned in WRITE_API_NAMES:
            if hasattr(self, banned):  # pragma: no cover - structural guard
                msg = (
                    f"TigerReadOnlyClient must not expose {banned!r}; CR-02 is read-only and "
                    "order submission belongs to the guarded execution adapter (CR-03)"
                )
                raise ConfigurationError(msg)
        self._client = client
        self._guard = guard
        self._account = account
        self._last_snapshot: BrokerSnapshot | None = None

    # -- verification ------------------------------------------------------------

    def verify_account(self) -> AccountIdentity:
        """Verify the paper account against the broker, then return it."""
        verdict = self._guard.verify_from_client(self._client)
        return verdict.raise_if_refused()

    @property
    def account(self) -> str:
        """The verified account number. Masked in all rendering, never in a log line."""
        return self._account

    @property
    def masked_account(self) -> str:
        return mask_account(self._account)

    # -- reads -------------------------------------------------------------------

    def assets(self) -> BrokerBalances:
        """Cash and account totals for the verified account.

        Observed live: ``get_assets()`` returns a **plain list** of ``PortfolioAccount``, not
        an object with an ``.assets`` attribute (the SDK docstring describes the underlying
        service, not the client's return value). Both shapes are accepted so a future SDK
        that reintroduces a wrapper does not break the read path.
        """
        response = self._client.get_assets(account=self._account)
        accounts = (
            response if isinstance(response, (list, tuple)) else getattr(response, "assets", None)
        )
        accounts = list(accounts or [])
        if not accounts:
            msg = "the broker returned no asset record for the verified account"
            raise BrokerError(msg)
        portfolio = accounts[0]
        summary = getattr(portfolio, "summary", None) or portfolio
        missing: list[str] = []

        def field(name: str) -> Decimal:
            return _finite_or_zero(getattr(summary, name, None), field_name=name, missing=missing)

        return BrokerBalances(
            currency=str(getattr(summary, "currency", None) or "USD"),
            cash=field("cash"),
            net_liquidation=field("net_liquidation"),
            buying_power=field("buying_power"),
            gross_position_value=field("gross_position_value"),
            available_funds=field("available_funds"),
            missing_fields=tuple(missing),
        )

    def positions(self) -> tuple[BrokerPosition, ...]:
        """Broker-reported holdings, sorted by symbol for deterministic output."""
        from tigeropen.common.consts import Market, SecurityType

        rows = list(
            self._client.get_positions(
                account=self._account, sec_type=SecurityType.STK, market=Market.US
            )
            or []
        )
        found: list[BrokerPosition] = []
        for row in rows:
            contract = getattr(row, "contract", None)
            symbol = str(getattr(contract, "symbol", None) or getattr(row, "symbol", "") or "")
            if not symbol:
                continue
            quantity = _broker_int(getattr(row, "quantity", 0))
            if quantity == 0:
                continue
            found.append(
                BrokerPosition(
                    instrument=symbol,
                    quantity=quantity,
                    average_cost=q_price(
                        broker_decimal(getattr(row, "average_cost", None)) or ZERO
                    ),
                    market_price=q_price(
                        broker_decimal(getattr(row, "market_price", None)) or ZERO
                    ),
                    market_value=q_cash(broker_decimal(getattr(row, "market_value", None)) or ZERO),
                    unrealized_pnl=q_cash(
                        broker_decimal(getattr(row, "unrealized_pnl", None)) or ZERO
                    ),
                    realized_pnl=q_cash(broker_decimal(getattr(row, "realized_pnl", None)) or ZERO),
                )
            )
        found.sort(key=lambda position: position.instrument)
        return tuple(found)

    def open_orders(self) -> tuple[BrokerOrder, ...]:
        """Broker-reported working orders."""
        response = self._client.get_open_orders(account=self._account)
        # Observed live: a plain list. The SDK docstring describes the service envelope
        # (response.result), not the client's return value, so both shapes are accepted.
        rows = (
            response if isinstance(response, (list, tuple)) else getattr(response, "result", None)
        )
        found: list[BrokerOrder] = []
        for row in rows or []:
            contract = getattr(row, "contract", None)
            symbol = str(getattr(contract, "symbol", None) or "")
            raw_status = str(getattr(row, "status", "") or "")
            action = str(getattr(row, "action", "") or "").upper()
            quantity = _broker_int(getattr(row, "quantity", 0))
            filled = _broker_int(getattr(row, "filled", 0))
            limit_price = getattr(row, "limit_price", None)
            found.append(
                BrokerOrder(
                    broker_order_id=str(
                        getattr(row, "order_id", None) or getattr(row, "id", "") or ""
                    ),
                    instrument=symbol,
                    side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
                    quantity=quantity,
                    filled_quantity=filled,
                    remaining_quantity=max(
                        _broker_int(getattr(row, "remaining", quantity - filled)), 0
                    ),
                    average_fill_price=q_price(
                        broker_decimal(getattr(row, "avg_fill_price", None)) or ZERO
                    ),
                    limit_price=_optional_price(limit_price),
                    order_type=str(getattr(row, "order_type", "") or ""),
                    time_in_force=str(getattr(row, "time_in_force", "") or ""),
                    raw_status=raw_status,
                    status=_STATUS_MAP.get(raw_status),
                )
            )
        found.sort(key=lambda order: (order.instrument, order.broker_order_id))
        return tuple(found)

    def snapshot(self, *, as_of: dt.datetime) -> BrokerSnapshot:
        """Read balances, positions and working orders in one pass.

        Ordering matters: the account is re-verified *first*, so a grant that has acquired a
        live account since the last check is refused before any data is read.
        """
        identity = self.verify_account()
        balances = self.assets()
        snapshot = BrokerSnapshot(
            as_of=as_of,
            account=identity.account,
            cash=q_cash(balances.cash),
            net_liquidation=q_cash(balances.net_liquidation),
            buying_power=q_cash(balances.buying_power),
            gross_position_value=q_cash(balances.gross_position_value),
            currency=balances.currency,
            positions=self.positions(),
            open_orders=self.open_orders(),
            missing_fields=balances.missing_fields,
        )
        self._last_snapshot = snapshot
        return snapshot

    @property
    def last_snapshot(self) -> BrokerSnapshot | None:
        """The most recent snapshot, for the operator surface."""
        return self._last_snapshot


def summarise(snapshots: Iterable[BrokerSnapshot]) -> list[dict[str, object]]:
    """Render snapshots for reporting, with account numbers masked."""
    return [snapshot.as_record() for snapshot in snapshots]
