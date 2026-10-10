"""Order-intent state machine, projection idempotency, and illegal transitions."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import (
    Mode,
    OrderSide,
    OrderStatus,
    OrderType,
    ReasonCode,
    RiskOutcomeKind,
    TimeInForce,
)
from tigerquant.core.errors import IllegalOrderTransition
from tigerquant.core.ids import IdFactory
from tigerquant.events.emitter import EventEmitter
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
from tigerquant.events.journal import MemoryJournal
from tigerquant.events.risk import RiskEvaluated
from tigerquant.execution.intent import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    OrderProjection,
    validate_transition,
)

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


@pytest.fixture
def projection() -> OrderProjection:
    return OrderProjection()


@pytest.fixture
def emitter(projection: OrderProjection) -> EventEmitter:
    clock = SimulatedClock(AT)
    return EventEmitter(
        mode=Mode.BACKTEST,
        sink=MemoryJournal(Mode.BACKTEST),
        ids=IdFactory("bt", clock),
        clock=clock,
        correlation_id="test",
        observers=(projection.apply,),
    )


def create_intent(emitter: EventEmitter, *, quantity: int = 10) -> str:
    event = emitter.emit(
        OrderIntentCreated,
        occurred_at=AT,
        intent_id="bt-oi_1",
        decision_id="bt-dec_1",
        instrument="SPY",
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        client_order_id="TQ-bt-1",
    )
    return event.intent_id


def risk_allow(emitter: EventEmitter, intent_id: str) -> None:
    emitter.emit(
        RiskEvaluated,
        occurred_at=AT,
        intent_id=intent_id,
        outcome=RiskOutcomeKind.ALLOW,
        reason_codes=(),
        policy_hash="sha256:" + "a" * 64,
        portfolio_digest="d",
        checks_run=("a",),
        detail="ok",
    )


def risk_deny(emitter: EventEmitter, intent_id: str) -> None:
    emitter.emit(
        RiskEvaluated,
        occurred_at=AT,
        intent_id=intent_id,
        outcome=RiskOutcomeKind.DENY,
        reason_codes=(ReasonCode.MARK_STALE,),
        policy_hash="sha256:" + "a" * 64,
        portfolio_digest="d",
        checks_run=("a",),
        detail="stale",
    )


def submit(emitter: EventEmitter, intent_id: str) -> None:
    emitter.emit(
        OrderSubmitted, occurred_at=AT, intent_id=intent_id, attempt=1, client_order_id="TQ-bt-1"
    )


def acknowledge(emitter: EventEmitter, intent_id: str, *, remaining: int = 10) -> None:
    emitter.emit(
        OrderAcknowledged,
        occurred_at=AT,
        intent_id=intent_id,
        broker_order_id="SIM-1",
        status=OrderStatus.ACKNOWLEDGED,
        remaining_quantity=remaining,
    )


class TestTransitionTable:
    def test_terminal_states_have_no_exits(self) -> None:
        for status in TERMINAL_STATUSES:
            assert ALLOWED_TRANSITIONS[status] == frozenset()

    def test_no_path_from_submit_unknown_back_to_submitting(self) -> None:
        assert OrderStatus.SUBMITTING not in ALLOWED_TRANSITIONS[OrderStatus.SUBMIT_UNKNOWN]

    def test_submit_unknown_can_only_be_reconciled_or_quarantined(self) -> None:
        assert ALLOWED_TRANSITIONS[OrderStatus.SUBMIT_UNKNOWN] == frozenset(
            {OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED, OrderStatus.QUARANTINED}
        )

    def test_illegal_transition_raises(self) -> None:
        with pytest.raises(IllegalOrderTransition, match="illegal order transition"):
            validate_transition(OrderStatus.CREATED, OrderStatus.FILLED)

    def test_partial_fill_may_repeat(self) -> None:
        validate_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED)


class TestProjection:
    def test_full_lifecycle(self, emitter: EventEmitter, projection: OrderProjection) -> None:
        intent_id = create_intent(emitter)
        risk_allow(emitter, intent_id)
        submit(emitter, intent_id)
        acknowledge(emitter, intent_id)
        emitter.emit(
            OrderFilled,
            occurred_at=AT,
            intent_id=intent_id,
            broker_order_id="SIM-1",
            exec_id="exec-1",
            quantity=10,
            price=Decimal("600.00"),
            commission=Decimal("0.05"),
            remaining_quantity=0,
        )
        snapshot = projection.get(intent_id)
        assert snapshot is not None
        assert snapshot.status is OrderStatus.FILLED
        assert snapshot.filled_quantity == 10
        assert snapshot.average_fill_price == Decimal("600.0000")
        assert not snapshot.is_open

    def test_duplicate_fill_does_not_double_book(
        self, emitter: EventEmitter, projection: OrderProjection
    ) -> None:
        intent_id = create_intent(emitter)
        risk_allow(emitter, intent_id)
        submit(emitter, intent_id)
        acknowledge(emitter, intent_id)
        emitter.emit(
            OrderPartiallyFilled,
            occurred_at=AT,
            intent_id=intent_id,
            broker_order_id="SIM-1",
            exec_id="exec-dup",
            quantity=4,
            price=Decimal("600.00"),
            commission=Decimal(0),
            remaining_quantity=6,
        )
        # Same exec id delivered twice.
        emitter.emit(
            OrderPartiallyFilled,
            occurred_at=AT,
            intent_id=intent_id,
            broker_order_id="SIM-1",
            exec_id="exec-dup",
            quantity=4,
            price=Decimal("600.00"),
            commission=Decimal(0),
            remaining_quantity=6,
        )
        snapshot = projection.get(intent_id)
        assert snapshot is not None
        assert snapshot.filled_quantity == 4
        assert snapshot.exec_ids == ("exec-dup",)

    def test_duplicate_event_id_is_ignored(self, emitter: EventEmitter) -> None:
        create_intent(emitter)
        created = emitter._sink.records[-1].event
        fresh = OrderProjection()
        assert fresh.apply(created) is True
        assert fresh.apply(created) is False

    def test_denied_risk_marks_the_intent_rejected(
        self, emitter: EventEmitter, projection: OrderProjection
    ) -> None:
        intent_id = create_intent(emitter)
        risk_deny(emitter, intent_id)
        snapshot = projection.get(intent_id)
        assert snapshot is not None
        assert snapshot.status is OrderStatus.REJECTED
        assert snapshot.reason_code is ReasonCode.MARK_STALE

    def test_unknown_outcome_blocks_new_exposure(
        self, emitter: EventEmitter, projection: OrderProjection
    ) -> None:
        intent_id = create_intent(emitter)
        risk_allow(emitter, intent_id)
        submit(emitter, intent_id)
        emitter.emit(
            OrderSubmissionUnknown,
            occurred_at=AT,
            intent_id=intent_id,
            reason_code=ReasonCode.SUBMIT_TIMEOUT,
            detail="timeout",
            attempt=1,
            reconcile_deadline=AT + dt.timedelta(minutes=5),
        )
        assert projection.blocking_intents()[0].intent_id == intent_id

    def test_quarantine_is_reachable_from_unknown(
        self, emitter: EventEmitter, projection: OrderProjection
    ) -> None:
        intent_id = create_intent(emitter)
        risk_allow(emitter, intent_id)
        submit(emitter, intent_id)
        emitter.emit(
            OrderSubmissionUnknown,
            occurred_at=AT,
            intent_id=intent_id,
            reason_code=ReasonCode.SUBMIT_TIMEOUT,
            detail="timeout",
            attempt=1,
            reconcile_deadline=AT,
        )
        emitter.emit(
            OrderQuarantined,
            occurred_at=AT,
            intent_id=intent_id,
            reason_code=ReasonCode.UNRECONCILED,
            detail="no venue record",
        )
        snapshot = projection.get(intent_id)
        assert snapshot is not None
        assert snapshot.status is OrderStatus.QUARANTINED
        assert snapshot.blocks_new_exposure

    def test_cancel_path(self, emitter: EventEmitter, projection: OrderProjection) -> None:
        intent_id = create_intent(emitter)
        risk_allow(emitter, intent_id)
        submit(emitter, intent_id)
        acknowledge(emitter, intent_id, remaining=6)
        emitter.emit(
            OrderCancelRequested,
            occurred_at=AT,
            intent_id=intent_id,
            client_order_id="TQ-bt-1",
            reason_code=ReasonCode.RISK_HALTED,
        )
        emitter.emit(
            OrderCanceled,
            occurred_at=AT,
            intent_id=intent_id,
            broker_order_id="SIM-1",
            canceled_quantity=6,
            reason_code=ReasonCode.RISK_HALTED,
        )
        snapshot = projection.get(intent_id)
        assert snapshot is not None
        assert snapshot.status is OrderStatus.CANCELED

    def test_rejection_after_acknowledgement_is_legal(
        self, emitter: EventEmitter, projection: OrderProjection
    ) -> None:
        intent_id = create_intent(emitter)
        risk_allow(emitter, intent_id)
        submit(emitter, intent_id)
        acknowledge(emitter, intent_id)
        emitter.emit(
            OrderRejected,
            occurred_at=AT,
            intent_id=intent_id,
            reason_code=ReasonCode.BROKER_REJECTED,
            broker_message="venue refused",
            broker_order_id="SIM-1",
        )
        snapshot = projection.get(intent_id)
        assert snapshot is not None
        assert snapshot.status is OrderStatus.REJECTED

    def test_projection_output_is_ordered_by_intent_id(
        self, emitter: EventEmitter, projection: OrderProjection
    ) -> None:
        create_intent(emitter)
        projection.apply(emitter._sink.records[-1].event)
        assert [s.intent_id for s in projection.snapshots()] == ["bt-oi_1"]

    def test_event_for_unknown_intent_raises(self, emitter: EventEmitter) -> None:
        # The projection is an observer, so the failure surfaces at emit time.
        with pytest.raises(IllegalOrderTransition, match="no order intent"):
            emitter.emit(
                OrderSubmitted,
                occurred_at=AT,
                intent_id="bt-oi_missing",
                attempt=1,
                client_order_id="x",
            )
