"""Execution-plane events (ADR-0008).

The state machine is expressed here as events, not as mutation: the current state of an
order intent is always reconstructible by folding these events in ``seq`` order.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from tigerquant.core.enums import OrderSide, OrderStatus, OrderType, ReasonCode, TimeInForce
from tigerquant.events.base import (
    AwareUTC,
    EventBase,
    NonEmptyStr,
    NonNegativeInt,
    PositiveDecimal,
    PositiveInt,
)

__all__ = [
    "OrderAcknowledged",
    "OrderCancelRequested",
    "OrderCanceled",
    "OrderFilled",
    "OrderIntentCreated",
    "OrderPartiallyFilled",
    "OrderQuarantined",
    "OrderRejected",
    "OrderSubmissionUnknown",
    "OrderSubmitted",
]


class OrderIntentCreated(EventBase):
    """A durable intent. Journaled *before* any broker interaction (ADR-0008 §1)."""

    event_type: Literal["OrderIntentCreated"] = "OrderIntentCreated"

    intent_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument: NonEmptyStr
    side: OrderSide
    quantity: PositiveInt
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: Decimal | None = Field(default=None, gt=0)
    #: Stable across a retry after an unknown outcome; derived from ``intent_id``.
    client_order_id: NonEmptyStr


class OrderSubmitted(EventBase):
    """The request left our process. Whether the broker received it is unknown here."""

    event_type: Literal["OrderSubmitted"] = "OrderSubmitted"

    intent_id: NonEmptyStr
    attempt: PositiveInt
    client_order_id: NonEmptyStr


class OrderAcknowledged(EventBase):
    """The broker confirmed acceptance and gave us its own identifier."""

    event_type: Literal["OrderAcknowledged"] = "OrderAcknowledged"

    intent_id: NonEmptyStr
    broker_order_id: NonEmptyStr
    status: OrderStatus
    remaining_quantity: NonNegativeInt


class OrderSubmissionUnknown(EventBase):
    """Timeout or transport failure after a request that may have been accepted.

    No automatic resubmission is ever performed from this state.
    """

    event_type: Literal["OrderSubmissionUnknown"] = "OrderSubmissionUnknown"

    intent_id: NonEmptyStr
    reason_code: ReasonCode
    detail: str
    attempt: PositiveInt
    #: Deadline after which unresolved intents are quarantined.
    reconcile_deadline: AwareUTC


class OrderRejected(EventBase):
    event_type: Literal["OrderRejected"] = "OrderRejected"

    intent_id: NonEmptyStr
    reason_code: ReasonCode
    broker_message: str
    broker_order_id: str | None = None


class OrderPartiallyFilled(EventBase):
    event_type: Literal["OrderPartiallyFilled"] = "OrderPartiallyFilled"

    intent_id: NonEmptyStr
    broker_order_id: NonEmptyStr
    #: Broker execution identifier — the idempotency key for fill booking.
    exec_id: NonEmptyStr
    quantity: PositiveInt
    price: PositiveDecimal
    commission: Decimal = Field(default=Decimal(0), ge=0)
    remaining_quantity: NonNegativeInt


class OrderFilled(EventBase):
    event_type: Literal["OrderFilled"] = "OrderFilled"

    intent_id: NonEmptyStr
    broker_order_id: NonEmptyStr
    exec_id: NonEmptyStr
    quantity: PositiveInt
    price: PositiveDecimal
    commission: Decimal = Field(default=Decimal(0), ge=0)
    remaining_quantity: NonNegativeInt = 0


class OrderCancelRequested(EventBase):
    """We asked the venue to cancel. The outcome is not yet known."""

    event_type: Literal["OrderCancelRequested"] = "OrderCancelRequested"

    intent_id: NonEmptyStr
    client_order_id: NonEmptyStr
    reason_code: ReasonCode


class OrderCanceled(EventBase):
    event_type: Literal["OrderCanceled"] = "OrderCanceled"

    intent_id: NonEmptyStr
    broker_order_id: NonEmptyStr
    canceled_quantity: NonNegativeInt
    reason_code: ReasonCode


class OrderQuarantined(EventBase):
    """Terminal-until-reviewed. Blocks new exposure while any intent is quarantined."""

    event_type: Literal["OrderQuarantined"] = "OrderQuarantined"

    intent_id: NonEmptyStr
    reason_code: ReasonCode
    detail: str
    requires_human_clear: bool = True
