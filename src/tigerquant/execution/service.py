"""Execution service: the only code that talks to a broker transport.

Responsibilities:

* consume a single-use risk approval, journal the attempt, call the broker;
* translate broker outcomes into journaled events and book fills into the ledger;
* reconcile unresolved intents — including the ``SUBMIT_UNKNOWN`` case — without ever
  resubmitting;
* hold the :class:`StartupGate` closed until reconciliation has run, so a restart cannot
  open new exposure before resting orders are accounted for (ADR-0008 §4).

It never decides *whether* to trade, and it accepts only an
:class:`~tigerquant.risk.approvals.ApprovedOrder` token.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.enums import (
    OrderSide,
    OrderStatus,
    ReasonCode,
    RiskOutcomeKind,
    Severity,
    SubmissionTarget,
    resolve_submission_target,
)
from tigerquant.core.errors import ModeViolation, RiskDenied
from tigerquant.events.execution import (
    OrderAcknowledged,
    OrderCanceled,
    OrderCancelRequested,
    OrderFilled,
    OrderPartiallyFilled,
    OrderQuarantined,
    OrderRejected,
    OrderSubmissionUnknown,
    OrderSubmitted,
)
from tigerquant.events.portfolio import PositionChanged
from tigerquant.events.risk import AlertRaised, RiskEvaluated
from tigerquant.execution.broker import BrokerOrderState

if TYPE_CHECKING:
    from collections.abc import Callable

    from tigerquant.core.enums import ModeCapabilities
    from tigerquant.core.ids import IdFactory
    from tigerquant.events.emitter import EventEmitter
    from tigerquant.execution.broker import BrokerAck, BrokerFill, BrokerProtocol
    from tigerquant.execution.intent import OrderProjection
    from tigerquant.portfolio.ledger import PortfolioLedger
    from tigerquant.risk.approvals import ApprovalRegistry
    from tigerquant.risk.gateway import RiskGateway

__all__ = ["ExecutionService", "ReconcileReport", "StartupGate", "SubmitResult"]


class StartupGate:
    """Blocks new exposure until reconciliation has completed.

    Deliberately starts closed and can only be opened by an explicit, journaled
    reconciliation pass.
    """

    __slots__ = ("_open", "_reason")

    def __init__(self, *, reason: str = "startup reconciliation has not run") -> None:
        self._open = False
        self._reason = reason

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def reason(self) -> str:
        return self._reason

    def open(self, *, reason: str) -> None:
        self._open = True
        self._reason = reason

    def close(self, *, reason: str) -> None:
        self._open = False
        self._reason = reason


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """What happened when we tried to submit."""

    intent_id: str
    accepted: bool
    state: BrokerOrderState | None
    fills: tuple[BrokerFill, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Outcome of a reconciliation pass."""

    resolved: tuple[str, ...]
    quarantined: tuple[str, ...]
    unresolved: tuple[str, ...]
    gate_opened: bool
    detail: str = ""


class ExecutionService:
    """Broker-facing orchestration. One instance per mode, per process."""

    __slots__ = (
        "_broker",
        "_emitter",
        "_gate",
        "_gateway",
        "_ids",
        "_ledger",
        "_mode_caps",
        "_projection",
        "_reconcile_deadline_seconds",
        "_submission_target",
    )

    def __init__(
        self,
        *,
        broker: BrokerProtocol,
        gateway: RiskGateway,
        emitter: EventEmitter,
        ledger: PortfolioLedger,
        projection: OrderProjection,
        gate: StartupGate,
        ids: IdFactory,
        mode_caps: ModeCapabilities,
        submission_target: SubmissionTarget | None = None,
        reconcile_deadline_seconds: int = 300,
    ) -> None:
        self._broker = broker
        self._gateway = gateway
        self._emitter = emitter
        self._ledger = ledger
        self._projection = projection
        self._gate = gate
        self._ids = ids
        self._mode_caps = mode_caps
        self._submission_target = (
            submission_target
            if submission_target is not None
            else resolve_submission_target(mode_caps.mode, broker.implementation_status)
        )
        self._reconcile_deadline_seconds = reconcile_deadline_seconds

    @property
    def submission_target(self) -> SubmissionTarget:
        return self._submission_target

    @property
    def gate(self) -> StartupGate:
        return self._gate

    @property
    def registry(self) -> ApprovalRegistry:
        return self._gateway.registry

    @property
    def broker(self) -> BrokerProtocol:
        return self._broker

    # -- submission --------------------------------------------------------------

    def submit(self, approval_token: str, *, at: dt.datetime) -> SubmitResult:
        """Submit a risk-approved order exactly once."""
        if self._submission_target is SubmissionTarget.NONE:
            msg = (
                f"mode {self._mode_caps.mode.value} resolves to submission target NONE; "
                "refusing to construct a broker request"
            )
            raise ModeViolation(msg)
        if not self._gate.is_open:
            msg = f"startup gate is closed: {self._gate.reason}"
            raise RiskDenied(msg, [ReasonCode.STARTUP_GATE_CLOSED])

        approval = self._gateway.registry.consume(approval_token)
        order = approval.order
        current = self._projection.get(order.intent_id)
        attempt = 1 if current is None else current.attempts + 1

        # Journal the gateway's verdict as the act of submitting it. The allowance is
        # journaled exactly once, by the component that acts on it, which keeps the order
        # state machine consistent even if a caller forgets to record it elsewhere.
        self._emitter.emit(
            RiskEvaluated,
            intent_id=order.intent_id,
            outcome=RiskOutcomeKind.ALLOW,
            reason_codes=(),
            policy_hash=approval.policy_hash,
            portfolio_digest=approval.portfolio_digest,
            checks_run=("submission_gate",),
            detail=f"allowed by policy {approval.policy_hash[:18]}…",
            occurred_at=at,
        )
        self._emitter.emit(
            OrderSubmitted,
            intent_id=order.intent_id,
            attempt=attempt,
            client_order_id=order.client_order_id,
            occurred_at=at,
        )

        ack: BrokerAck = self._broker.submit(approval, at=at)

        if ack.state is BrokerOrderState.UNKNOWN:
            # The request was transmitted; the outcome is not known. Never retried here.
            self._emitter.emit(
                OrderSubmissionUnknown,
                intent_id=order.intent_id,
                reason_code=ReasonCode.SUBMIT_TIMEOUT,
                detail=ack.message or "no acknowledgement received",
                attempt=attempt,
                reconcile_deadline=at + dt.timedelta(seconds=self._reconcile_deadline_seconds),
                occurred_at=at,
            )
            return SubmitResult(
                intent_id=order.intent_id,
                accepted=False,
                state=ack.state,
                fills=(),
                detail=ack.message,
            )

        if ack.state is BrokerOrderState.REJECTED:
            self._emitter.emit(
                OrderRejected,
                intent_id=order.intent_id,
                reason_code=ReasonCode.BROKER_REJECTED,
                broker_message=ack.message,
                broker_order_id=ack.broker_order_id,
                occurred_at=at,
            )
            return SubmitResult(
                intent_id=order.intent_id,
                accepted=False,
                state=ack.state,
                fills=(),
                detail=ack.message,
            )

        self._journal_acknowledgement(
            intent_id=order.intent_id,
            broker_order_id=ack.broker_order_id or "",
            # The venue's ``remaining`` is measured *after* the fills in this response.
            # The acknowledgement must state the position before those fills, so the
            # projection can classify each fill correctly (a 4-of-8 partial must not be
            # booked as a complete fill).
            remaining_before_fills=order.quantity,
            at=at,
        )
        fills = self._book_fills(
            intent_id=order.intent_id,
            instrument=order.instrument,
            side=order.side,
            fills=ack.fills,
            at=at,
        )
        return SubmitResult(
            intent_id=order.intent_id,
            accepted=True,
            state=ack.state,
            fills=fills,
            detail=ack.message,
        )

    def cancel(self, intent_id: str, *, at: dt.datetime, reason_code: ReasonCode) -> int:
        """Cancel the remainder of a resting order. Returns the canceled quantity."""
        snapshot = self._projection.get(intent_id)
        if snapshot is None:
            msg = f"no order intent {intent_id!r} to cancel"
            raise ModeViolation(msg)
        self._emitter.emit(
            OrderCancelRequested,
            intent_id=intent_id,
            client_order_id=snapshot.client_order_id,
            reason_code=reason_code,
            occurred_at=at,
        )
        result = self._broker.cancel(client_order_id=snapshot.client_order_id, at=at)
        if result.state is BrokerOrderState.UNKNOWN:
            self._emitter.emit(
                OrderQuarantined,
                intent_id=intent_id,
                reason_code=ReasonCode.UNRECONCILED,
                detail=f"cancel outcome unknown: {result.message}",
                occurred_at=at,
            )
            return 0
        self._emitter.emit(
            OrderCanceled,
            intent_id=intent_id,
            broker_order_id=snapshot.broker_order_id or "",
            canceled_quantity=result.canceled_quantity,
            reason_code=reason_code,
            occurred_at=at,
        )
        return result.canceled_quantity

    # -- reconciliation ----------------------------------------------------------

    def startup_reconcile(self, *, at: dt.datetime) -> ReconcileReport:
        """Reconcile every non-terminal intent before allowing new exposure."""
        return self._reconcile(at=at, opening_gate=True)

    def reconcile(self, *, at: dt.datetime) -> ReconcileReport:
        """Reconcile non-terminal intents; the gate keeps whatever state it has."""
        return self._reconcile(at=at, opening_gate=False)

    def _reconcile(self, *, at: dt.datetime, opening_gate: bool) -> ReconcileReport:
        resolved: list[str] = []
        quarantined: list[str] = []
        unresolved: list[str] = []

        for snapshot in self._projection.open_intents():
            view = self._broker.query(client_order_id=snapshot.client_order_id)
            if view is None:
                if snapshot.status is OrderStatus.SUBMIT_UNKNOWN:
                    # No deadline is stored on the projection, so an unresolved unknown
                    # intent is quarantined immediately rather than waited on: the safe
                    # direction is to stop, not to keep trying.
                    self._emitter.emit(
                        OrderQuarantined,
                        intent_id=snapshot.intent_id,
                        reason_code=ReasonCode.UNRECONCILED,
                        detail=(
                            "the venue has no record of this order after an unknown "
                            "submission outcome; a human must confirm no order exists"
                        ),
                        occurred_at=at,
                    )
                    quarantined.append(snapshot.intent_id)
                    continue
                unresolved.append(snapshot.intent_id)
                continue

            if view.state is BrokerOrderState.REJECTED:
                self._emitter.emit(
                    OrderRejected,
                    intent_id=snapshot.intent_id,
                    reason_code=ReasonCode.BROKER_REJECTED,
                    broker_message="venue reports the order was rejected",
                    broker_order_id=view.broker_order_id,
                    occurred_at=at,
                )
                resolved.append(snapshot.intent_id)
                continue

            self._journal_acknowledgement(
                intent_id=snapshot.intent_id,
                broker_order_id=view.broker_order_id or "",
                remaining_before_fills=max(snapshot.quantity - snapshot.filled_quantity, 0),
                at=at,
            )
            self._book_fills(
                intent_id=snapshot.intent_id,
                instrument=snapshot.instrument,
                side=snapshot.side,
                fills=view.fills,
                at=at,
            )
            if view.state is BrokerOrderState.CANCELED:
                self._emitter.emit(
                    OrderCanceled,
                    intent_id=snapshot.intent_id,
                    broker_order_id=view.broker_order_id or "",
                    canceled_quantity=view.remaining,
                    reason_code=ReasonCode.UNRECONCILED,
                    occurred_at=at,
                )
            resolved.append(snapshot.intent_id)

        still_blocking = self._projection.blocking_intents()
        gate_opened = False
        if opening_gate and not still_blocking:
            self._gate.open(reason=f"startup reconciliation completed at {at.isoformat()}")
            gate_opened = True
        elif still_blocking:
            self._emitter.emit(
                AlertRaised,
                severity=Severity.CRITICAL,
                code=ReasonCode.UNRECONCILED,
                message=(
                    f"{len(still_blocking)} order intent(s) are unresolved; "
                    "new exposure remains blocked"
                ),
                context={"intents": ",".join(s.intent_id for s in still_blocking)},
                occurred_at=at,
            )
        return ReconcileReport(
            resolved=tuple(resolved),
            quarantined=tuple(quarantined),
            unresolved=tuple(unresolved),
            gate_opened=gate_opened,
            detail=(
                f"resolved={len(resolved)} quarantined={len(quarantined)} "
                f"unresolved={len(unresolved)}"
            ),
        )

    # -- internals ---------------------------------------------------------------

    def _journal_acknowledgement(
        self,
        *,
        intent_id: str,
        broker_order_id: str,
        remaining_before_fills: int,
        at: dt.datetime,
    ) -> None:
        snapshot = self._projection.get(intent_id)
        if snapshot is None:
            return
        if snapshot.status in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
            # Already acknowledged; later updates are carried by fill events.
            return
        self._emitter.emit(
            OrderAcknowledged,
            intent_id=intent_id,
            broker_order_id=broker_order_id or "unknown",
            status=OrderStatus.ACKNOWLEDGED,
            remaining_quantity=max(remaining_before_fills, 0),
            occurred_at=at,
        )

    def _book_fills(
        self,
        *,
        intent_id: str,
        instrument: str,
        side: OrderSide,
        fills: tuple[BrokerFill, ...],
        at: dt.datetime,
    ) -> tuple[BrokerFill, ...]:
        """Journal and book fills, skipping any that were already applied."""
        booked: list[BrokerFill] = []
        for fill in fills:
            snapshot = self._projection.get(intent_id)
            if snapshot is not None and fill.exec_id in snapshot.exec_ids:
                continue  # duplicate delivery: book nothing (acceptance gate 7)
            result = self._ledger.apply_fill(
                exec_id=fill.exec_id,
                instrument=instrument,
                side=side,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                occurred_at=fill.occurred_at,
            )
            if result is None:
                continue
            fills_event = (
                OrderFilled
                if snapshot is not None and fill.quantity >= snapshot.remaining_quantity
                else OrderPartiallyFilled
            )
            self._emitter.emit(
                fills_event,
                intent_id=intent_id,
                broker_order_id=fill.broker_order_id,
                exec_id=fill.exec_id,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                remaining_quantity=max(
                    (snapshot.remaining_quantity - fill.quantity) if snapshot else 0, 0
                ),
                occurred_at=at,
            )
            updated = self._projection.get(intent_id)
            mark = fill.price
            self._emitter.emit(
                PositionChanged,
                instrument=instrument,
                quantity=result.position.quantity,
                average_cost=result.position.average_cost,
                realized_pnl=result.position.realized_pnl,
                mark_price=mark,
                unrealized_pnl=result.position.unrealized_pnl(mark),
                occurred_at=at,
            )
            del updated
            booked.append(fill)
        return tuple(booked)


def make_mark_provider(
    marks: dict[str, Decimal],
) -> Callable[[str], Decimal | None]:
    """Expose a mark map to the fake broker as a lookup function."""

    def provider(instrument: str) -> Decimal | None:
        return marks.get(instrument)

    return provider
