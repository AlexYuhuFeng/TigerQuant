"""Deterministic fake broker.

Behaviour is fully specified by the scenario fixture, so every failure mode in
`docs/ACCEPTANCE_GATES.md` that concerns the broker can be reproduced exactly and
offline:

* ``immediate`` — accepted and filled in one step.
* ``partial`` — accepted, partially filled, remainder resting (for the partial-fill-then-cancel
  case, acceptance gate 5).
* ``next_bar`` — accepted, filled when the pipeline next polls (models asynchronous fills).
* ``reject`` — the venue refuses (acceptance gate 3 path).
* ``timeout`` — the request may have been received and the response was lost. The order is
  recorded internally as accepted, so reconciliation *can* find it, but the caller learns
  nothing. This is the case that must never produce a blind retry (acceptance gate 4).

Everything the broker does is a pure function of `(fixture, approvals, scenario clock)`,
so a replay is byte-identical.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.enums import ImplementationStatus, OrderSide, OrderType
from tigerquant.core.ids import stable_token
from tigerquant.core.money import q_cash, q_price, to_decimal
from tigerquant.execution.broker import (
    BrokerAck,
    BrokerCancel,
    BrokerFill,
    BrokerOrderState,
    BrokerOrderView,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tigerquant.market.fixtures import BrokerFixture
    from tigerquant.risk.approvals import ApprovedOrder

__all__ = ["DeterministicFakeBroker"]


class _OrderRecord:
    """Internal venue-side order state."""

    __slots__ = (
        "broker_order_id",
        "client_order_id",
        "fills",
        "instrument",
        "quantity",
        "remaining",
        "side",
        "state",
    )

    def __init__(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        instrument: str,
        side: OrderSide,
        quantity: int,
    ) -> None:
        self.client_order_id = client_order_id
        self.broker_order_id = broker_order_id
        self.instrument = instrument
        self.side = side
        self.quantity = quantity
        self.remaining = quantity
        self.fills: list[BrokerFill] = []
        self.state = BrokerOrderState.ACCEPTED

    @property
    def filled(self) -> int:
        return sum(fill.quantity for fill in self.fills)

    def view(self) -> BrokerOrderView:
        return BrokerOrderView(
            client_order_id=self.client_order_id,
            broker_order_id=self.broker_order_id,
            state=self.state,
            filled_quantity=self.filled,
            remaining=self.remaining,
            fills=tuple(self.fills),
        )


class DeterministicFakeBroker:
    """A broker that behaves exactly as the fixture says it does."""

    __slots__ = (
        "_fill_sequence",
        "_mark_provider",
        "_orders_by_broker_id",
        "_orders_by_client_id",
        "_pending_fills",
        "_spec",
    )

    def __init__(
        self,
        spec: BrokerFixture,
        *,
        mark_provider: Callable[[str], Decimal | None],
    ) -> None:
        self._spec = spec
        self._mark_provider = mark_provider
        self._orders_by_client_id: dict[str, _OrderRecord] = {}
        self._orders_by_broker_id: dict[str, _OrderRecord] = {}
        self._fill_sequence = 0
        self._pending_fills: list[str] = []

    # -- metadata ----------------------------------------------------------------

    @property
    def implementation_status(self) -> ImplementationStatus:
        return ImplementationStatus.SIMULATED

    @property
    def order_count(self) -> int:
        return len(self._orders_by_client_id)

    def broker_positions(self) -> dict[str, int]:
        """Venue-authoritative net position per instrument, for reconciliation tests."""
        positions: dict[str, int] = {}
        for record in self._orders_by_client_id.values():
            filled = record.filled
            if not filled:
                continue
            delta = filled if record.side is OrderSide.BUY else -filled
            positions[record.instrument] = positions.get(record.instrument, 0) + delta
        return positions

    # -- transport ---------------------------------------------------------------

    def submit(self, approval: ApprovedOrder, *, at: dt.datetime) -> BrokerAck:
        """Submit an approved order, honouring the fixture's fill policy."""
        order = approval.order
        client_order_id = order.client_order_id
        existing = self._orders_by_client_id.get(client_order_id)
        if existing is not None:
            # A venue that has already seen this client order id must not create a
            # second order. The fake broker mirrors that, so a double submit is visible.
            return BrokerAck(
                client_order_id=client_order_id,
                state=existing.state,
                broker_order_id=existing.broker_order_id,
                fills=(),
                remaining=existing.remaining,
                message="duplicate client order id rejected by the venue",
            )

        broker_order_id = self._broker_order_id(client_order_id)
        if order.order_type is OrderType.LIMIT and order.limit_price is None:
            msg = "a LIMIT order requires a limit price"
            raise ValueError(msg)

        record = _OrderRecord(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
        )
        self._orders_by_client_id[client_order_id] = record
        self._orders_by_broker_id[broker_order_id] = record

        policy = self._spec.fill_policy

        if policy == "reject":
            record.state = BrokerOrderState.REJECTED
            return BrokerAck(
                client_order_id=client_order_id,
                state=BrokerOrderState.REJECTED,
                broker_order_id=broker_order_id,
                remaining=record.remaining,
                message="fixture fill_policy=reject: venue refused the order",
            )

        if policy == "timeout":
            # The venue accepted it; the caller never learns that. Reconciliation is
            # the only way out (ADR-0008 §3).
            return BrokerAck(
                client_order_id=client_order_id,
                state=BrokerOrderState.UNKNOWN,
                broker_order_id=None,
                remaining=order.quantity,
                message="fixture fill_policy=timeout: response lost after transmission",
            )

        if policy == "partial":
            quantity = self._partial_quantity(order.quantity)
            fill = self._make_fill(record, quantity=quantity, at=at)
            record.fills.append(fill)
            record.remaining = order.quantity - quantity
            record.state = BrokerOrderState.PARTIALLY_FILLED
            return BrokerAck(
                client_order_id=client_order_id,
                state=record.state,
                broker_order_id=broker_order_id,
                fills=(fill,),
                remaining=record.remaining,
                message="partial fill; remainder resting",
            )

        if policy == "next_bar":
            self._pending_fills.append(client_order_id)
            record.state = BrokerOrderState.ACCEPTED
            return BrokerAck(
                client_order_id=client_order_id,
                state=record.state,
                broker_order_id=broker_order_id,
                fills=(),
                remaining=record.remaining,
                message="accepted; awaiting next bar",
            )

        fill = self._make_fill(record, quantity=order.quantity, at=at)
        record.fills.append(fill)
        record.remaining = 0
        record.state = BrokerOrderState.FILLED
        return BrokerAck(
            client_order_id=client_order_id,
            state=record.state,
            broker_order_id=broker_order_id,
            fills=(fill,),
            remaining=0,
            message="filled",
        )

    def cancel(self, *, client_order_id: str, at: dt.datetime) -> BrokerCancel:
        """Cancel the remainder of a resting order."""
        del at
        record = self._orders_by_client_id.get(client_order_id)
        if record is None:
            return BrokerCancel(
                client_order_id=client_order_id,
                state=BrokerOrderState.UNKNOWN,
                canceled_quantity=0,
                message="unknown client order id",
            )
        remaining = record.remaining
        if record.state in {BrokerOrderState.FILLED, BrokerOrderState.CANCELED}:
            return BrokerCancel(
                client_order_id=client_order_id,
                state=record.state,
                canceled_quantity=0,
                message="order is no longer cancellable",
            )
        record.remaining = 0
        record.state = BrokerOrderState.CANCELED
        if client_order_id in self._pending_fills:
            self._pending_fills.remove(client_order_id)
        return BrokerCancel(
            client_order_id=client_order_id,
            state=BrokerOrderState.CANCELED,
            canceled_quantity=remaining,
            message="canceled; remainder released",
        )

    def query(self, *, client_order_id: str) -> BrokerOrderView | None:
        record = self._orders_by_client_id.get(client_order_id)
        return None if record is None else record.view()

    def poll(self, *, at: dt.datetime) -> tuple[BrokerFill, ...]:
        """Release fills deferred by the ``next_bar`` policy."""
        released: list[BrokerFill] = []
        for client_order_id in list(self._pending_fills):
            record = self._orders_by_client_id[client_order_id]
            fill = self._make_fill(record, quantity=record.remaining, at=at)
            record.fills.append(fill)
            record.remaining = 0
            record.state = BrokerOrderState.FILLED
            self._pending_fills.remove(client_order_id)
            released.append(fill)
        return tuple(released)

    # -- internals ---------------------------------------------------------------

    def _broker_order_id(self, client_order_id: str) -> str:
        return f"SIM-{stable_token(client_order_id, 12).upper()}"

    def _partial_quantity(self, quantity: int) -> int:
        fraction = to_decimal(self._spec.partial_fill_fraction)
        partial = int((Decimal(quantity) * fraction).to_integral_value(rounding="ROUND_FLOOR"))
        # A partial fill must be a real, non-zero quantity and must leave a remainder.
        return max(1, min(partial, quantity - 1)) if quantity > 1 else quantity

    def _fill_price(self, record: _OrderRecord, at: dt.datetime) -> Decimal:
        del at
        mark = self._mark_provider(record.instrument)
        if mark is None:
            msg = (
                f"no mark price for {record.instrument}; the fake broker refuses to invent "
                "a fill price"
            )
            raise ValueError(msg)
        price = to_decimal(mark)
        slippage = to_decimal(self._spec.slippage_bps) / Decimal(10_000)
        if record.side is OrderSide.BUY:
            price = price * (Decimal(1) + slippage)
        else:
            price = price * (Decimal(1) - slippage)
        return q_price(price)

    def _make_fill(self, record: _OrderRecord, *, quantity: int, at: dt.datetime) -> BrokerFill:
        self._fill_sequence += 1
        price = self._fill_price(record, at)
        commission = q_cash(to_decimal(self._spec.commission_per_share) * quantity)
        return BrokerFill(
            exec_id=f"exec:{stable_token(f'{record.client_order_id}:{self._fill_sequence}', 20)}",
            quantity=quantity,
            price=price,
            commission=commission,
            occurred_at=at,
            broker_order_id=record.broker_order_id,
        )
