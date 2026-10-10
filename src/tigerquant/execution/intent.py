"""Order-intent state machine and projection (ADR-0008).

State is never mutated in place: the current status of an intent is folded from its
journaled events. That is what makes "reconstruct the order after a restart" a normal
operation rather than a recovery special case.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ConfigDict

from tigerquant.core.enums import OrderSide, OrderStatus, OrderType, ReasonCode, TimeInForce
from tigerquant.core.errors import IllegalOrderTransition
from tigerquant.core.money import ZERO, q_price
from tigerquant.events.execution import (
    OrderAcknowledged,
    OrderCanceled,
    OrderCancelRequested,
    OrderFilled,
    OrderIntentCreated,
    OrderPartiallyFilled,
    OrderQuarantined,
    OrderRejected,
    OrderSubmissionUnknown,
    OrderSubmitted,
)
from tigerquant.events.risk import RiskEvaluated

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tigerquant.events.base import EventBase

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "OrderProjection",
    "OrderSnapshot",
    "validate_transition",
]

#: Legal transitions. Anything absent is an illegal transition and raises.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {OrderStatus.RISK_APPROVED, OrderStatus.REJECTED, OrderStatus.QUARANTINED}
    ),
    OrderStatus.RISK_APPROVED: frozenset(
        {OrderStatus.SUBMITTING, OrderStatus.REJECTED, OrderStatus.QUARANTINED}
    ),
    OrderStatus.SUBMITTING: frozenset(
        {OrderStatus.ACKNOWLEDGED, OrderStatus.SUBMIT_UNKNOWN, OrderStatus.REJECTED}
    ),
    # Exits from SUBMIT_UNKNOWN are deliberately limited: only reconciliation may
    # resolve it, and there is no transition back to SUBMITTING.
    OrderStatus.SUBMIT_UNKNOWN: frozenset(
        {OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED, OrderStatus.QUARANTINED}
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.REJECTED,
            OrderStatus.QUARANTINED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.QUARANTINED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCELED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.QUARANTINED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    # A quarantined intent is terminal for autonomous operation but may be resolved by
    # an explicit, human-reviewed reconciliation.
    OrderStatus.QUARANTINED: frozenset(
        {OrderStatus.ACKNOWLEDGED, OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.REJECTED}
    ),
}

#: Statuses from which no autonomous action may follow.
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
)

#: Statuses that block new exposure while they exist.
BLOCKING_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.SUBMIT_UNKNOWN,
        OrderStatus.QUARANTINED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.PARTIALLY_FILLED,
    }
)


def validate_transition(current: OrderStatus, target: OrderStatus) -> None:
    """Raise :class:`IllegalOrderTransition` unless ``current -> target`` is legal."""
    if target is current and target in {OrderStatus.PARTIALLY_FILLED}:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        msg = (
            f"illegal order transition {current.value} -> {target.value}; "
            f"allowed from {current.value}: "
            f"{sorted(status.value for status in allowed) or 'none (terminal)'}"
        )
        raise IllegalOrderTransition(msg)


class OrderSnapshot(BaseModel):
    """The folded state of one order intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str
    decision_id: str
    instrument: str
    side: OrderSide
    quantity: int
    filled_quantity: int
    remaining_quantity: int
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: Decimal | None
    client_order_id: str
    status: OrderStatus
    broker_order_id: str | None = None
    average_fill_price: Decimal | None = None
    commission: Decimal = ZERO
    attempts: int = 0
    reason_code: ReasonCode | None = None
    exec_ids: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def blocks_new_exposure(self) -> bool:
        return self.status in BLOCKING_STATUSES


E = TypeVar("E", bound="EventBase")


class OrderProjection:
    """Folds order events into snapshots, idempotently.

    Idempotency is by ``exec_id`` for fills and by ``event_id`` for every event, so a
    duplicated broker push cannot double-book a fill (acceptance gate 7).
    """

    __slots__ = ("_seen_event_ids", "_snapshots")

    def __init__(self) -> None:
        self._snapshots: dict[str, OrderSnapshot] = {}
        self._seen_event_ids: set[str] = set()

    def snapshots(self) -> tuple[OrderSnapshot, ...]:
        """All known intents, ordered by id, for deterministic output."""
        return tuple(self._snapshots[key] for key in sorted(self._snapshots))

    def open_intents(self) -> tuple[OrderSnapshot, ...]:
        return tuple(snap for snap in self.snapshots() if snap.is_open)

    def blocking_intents(self) -> tuple[OrderSnapshot, ...]:
        return tuple(snap for snap in self.snapshots() if snap.blocks_new_exposure)

    def get(self, intent_id: str) -> OrderSnapshot | None:
        return self._snapshots.get(intent_id)

    def apply(self, event: EventBase) -> bool:
        """Apply one event. Returns ``False`` when it was already applied."""
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, str):
            msg = f"event {event!r} has no event_id"
            raise IllegalOrderTransition(msg)
        if event_id in self._seen_event_ids:
            return False

        if isinstance(event, OrderIntentCreated):
            self._snapshots[event.intent_id] = OrderSnapshot(
                intent_id=event.intent_id,
                decision_id=event.decision_id,
                instrument=event.instrument,
                side=event.side,
                quantity=event.quantity,
                filled_quantity=0,
                remaining_quantity=event.quantity,
                order_type=event.order_type,
                time_in_force=event.time_in_force,
                limit_price=event.limit_price,
                client_order_id=event.client_order_id,
                status=OrderStatus.CREATED,
            )
        elif isinstance(event, RiskEvaluated):
            current = self._snapshots.get(event.intent_id)
            if event.is_allowed():
                # Tolerate a repeated allowance: the verdict is idempotent per intent.
                if current is not None and current.status is not OrderStatus.CREATED:
                    return False
                self._transition(event.intent_id, OrderStatus.RISK_APPROVED, event.event_id)
            else:
                self._transition(
                    event.intent_id,
                    OrderStatus.REJECTED,
                    event.event_id,
                    reason_code=(
                        event.reason_codes[0] if event.reason_codes else ReasonCode.CHECK_ERROR
                    ),
                )
        elif isinstance(event, OrderSubmitted):
            self._transition(
                event.intent_id, OrderStatus.SUBMITTING, event.event_id, attempts=event.attempt
            )
        elif isinstance(event, OrderAcknowledged):
            self._transition(
                event.intent_id,
                event.status,
                event.event_id,
                broker_order_id=event.broker_order_id,
                remaining_quantity=event.remaining_quantity,
            )
        elif isinstance(event, OrderSubmissionUnknown):
            self._transition(
                event.intent_id,
                OrderStatus.SUBMIT_UNKNOWN,
                event.event_id,
                reason_code=event.reason_code,
            )
        elif isinstance(event, OrderRejected):
            self._transition(
                event.intent_id,
                OrderStatus.REJECTED,
                event.event_id,
                reason_code=event.reason_code,
                broker_order_id=event.broker_order_id,
            )
        elif isinstance(event, (OrderFilled, OrderPartiallyFilled)):
            self._apply_fill(event)
        elif isinstance(event, OrderCancelRequested):
            self._transition(event.intent_id, OrderStatus.CANCEL_PENDING, event.event_id)
        elif isinstance(event, OrderCanceled):
            self._transition(
                event.intent_id,
                OrderStatus.CANCELED,
                event.event_id,
                reason_code=event.reason_code,
                remaining_quantity=event.canceled_quantity,
            )
        elif isinstance(event, OrderQuarantined):
            self._transition(
                event.intent_id,
                OrderStatus.QUARANTINED,
                event.event_id,
                reason_code=event.reason_code,
            )
        else:
            return False

        self._seen_event_ids.add(event_id)
        return True

    def apply_all(self, events: Iterable[EventBase]) -> int:
        """Apply many events in order; returns how many were new."""
        return sum(1 for event in events if self.apply(event))

    # -- internals ---------------------------------------------------------------

    def _require(self, intent_id: str) -> OrderSnapshot:
        try:
            return self._snapshots[intent_id]
        except KeyError as exc:
            msg = f"no order intent {intent_id!r} has been created"
            raise IllegalOrderTransition(msg) from exc

    def _transition(
        self,
        intent_id: str,
        target: OrderStatus,
        event_id: str,
        *,
        reason_code: ReasonCode | None = None,
        broker_order_id: str | None = None,
        remaining_quantity: int | None = None,
        attempts: int | None = None,
    ) -> None:
        del event_id
        current = self._require(intent_id)
        validate_transition(current.status, target)
        update: dict[str, object] = {"status": target}
        if reason_code is not None:
            update["reason_code"] = reason_code
        if broker_order_id is not None:
            update["broker_order_id"] = broker_order_id
        if remaining_quantity is not None:
            update["remaining_quantity"] = remaining_quantity
        if attempts is not None:
            update["attempts"] = attempts
        self._snapshots[intent_id] = current.model_copy(update=update)

    def _apply_fill(self, event: OrderFilled | OrderPartiallyFilled) -> None:
        current = self._require(event.intent_id)
        if event.exec_id in current.exec_ids:
            # Duplicate broker fill: book nothing (acceptance gate 7).
            return
        filled = current.filled_quantity + event.quantity
        previous_notional = (current.average_fill_price or ZERO) * current.filled_quantity
        new_average = q_price((previous_notional + event.price * event.quantity) / filled)
        target = (
            OrderStatus.FILLED
            if event.remaining_quantity <= 0 or filled >= current.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        validate_transition(current.status, target)
        self._snapshots[event.intent_id] = current.model_copy(
            update={
                "status": target,
                "filled_quantity": filled,
                "remaining_quantity": max(current.quantity - filled, 0),
                "average_fill_price": new_average,
                "commission": current.commission + event.commission,
                "broker_order_id": event.broker_order_id,
                "exec_ids": (*current.exec_ids, event.exec_id),
            }
        )


def fold_order(events: Sequence[EventBase], intent_id: str) -> OrderSnapshot | None:
    """Convenience: fold a whole event list and return one intent's snapshot."""
    projection = OrderProjection()
    projection.apply_all(events)
    return projection.get(intent_id)
