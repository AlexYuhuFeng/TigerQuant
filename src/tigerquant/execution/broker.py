"""Broker transport contract and value types.

The broker is a *transport*: it accepts an :class:`~tigerquant.risk.approvals.ApprovedOrder`,
reports what the venue did, and can be queried for reconciliation. It does not decide
anything and it never sees a strategy, a model or a raw ``OrderIntent``.

Every broker event is returned as a value object; the execution service is responsible
for journaling it. That separation is what lets the same service drive the deterministic
fake broker in CR-01 and the guarded Tiger paper adapter in CR-03 without changing the
state machine.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tigerquant.core.enums import ImplementationStatus
    from tigerquant.risk.approvals import ApprovedOrder

__all__ = [
    "BrokerAck",
    "BrokerCancel",
    "BrokerFill",
    "BrokerOrderState",
    "BrokerOrderView",
    "BrokerProtocol",
]


class BrokerOrderState(StrEnum):
    """What the venue says about an order. ``UNKNOWN`` is a first-class outcome."""

    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    #: The request may or may not have been received. Never retried blindly.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BrokerFill:
    """One venue execution. ``exec_id`` is the idempotency key for booking."""

    exec_id: str
    quantity: int
    price: Decimal
    commission: Decimal
    occurred_at: dt.datetime
    broker_order_id: str


@dataclass(frozen=True, slots=True)
class BrokerAck:
    """Result of a submission attempt."""

    client_order_id: str
    state: BrokerOrderState
    broker_order_id: str | None = None
    fills: tuple[BrokerFill, ...] = ()
    remaining: int = 0
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.state in {
            BrokerOrderState.ACCEPTED,
            BrokerOrderState.PARTIALLY_FILLED,
            BrokerOrderState.FILLED,
        }


@dataclass(frozen=True, slots=True)
class BrokerCancel:
    """Result of a cancellation attempt."""

    client_order_id: str
    state: BrokerOrderState
    canceled_quantity: int
    message: str = ""


@dataclass(frozen=True, slots=True)
class BrokerOrderView:
    """Venue-authoritative view of one order, used by reconciliation."""

    client_order_id: str
    broker_order_id: str | None
    state: BrokerOrderState
    filled_quantity: int
    remaining: int
    fills: tuple[BrokerFill, ...] = field(default=())


@runtime_checkable
class BrokerProtocol(Protocol):
    """What the execution service requires of any broker transport."""

    @property
    def implementation_status(self) -> ImplementationStatus:
        """Honest label: ``SIMULATED``, ``PAPER_VERIFIED``, ... (invariant 11)."""
        ...

    def submit(self, approval: ApprovedOrder, *, at: dt.datetime) -> BrokerAck:
        """Attempt to submit an approved order."""
        ...

    def cancel(self, *, client_order_id: str, at: dt.datetime) -> BrokerCancel:
        """Attempt to cancel a resting order."""
        ...

    def query(self, *, client_order_id: str) -> BrokerOrderView | None:
        """Look an order up by our client order id. ``None`` means "not found"."""
        ...

    def poll(self, *, at: dt.datetime) -> tuple[BrokerFill, ...]:
        """Drain any fills that became available without a new request."""
        ...
