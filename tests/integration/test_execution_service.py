"""Execution service: approvals, startup gate, partial fills, duplicates, reconciliation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import (
    Mode,
    OrderSide,
    OrderType,
    ReasonCode,
    SubmissionTarget,
    TimeInForce,
    capabilities_for,
)
from tigerquant.core.errors import ModeViolation, RiskDenied
from tigerquant.core.ids import IdFactory
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.execution import OrderIntentCreated
from tigerquant.events.journal import MemoryJournal
from tigerquant.execution.broker import BrokerOrderState
from tigerquant.execution.fake_broker import DeterministicFakeBroker
from tigerquant.execution.intent import OrderProjection
from tigerquant.execution.service import ExecutionService, StartupGate
from tigerquant.market.fixtures import BrokerFixture
from tigerquant.market.session import TradingSession
from tigerquant.portfolio.ledger import PortfolioLedger
from tigerquant.risk.approvals import ApprovalRegistry, OrderIntentView
from tigerquant.risk.gateway import RiskGateway, RiskRequest
from tigerquant.risk.policy import RiskPolicy
from tigerquant.risk.state import RiskState

if TYPE_CHECKING:
    from tigerquant.market.fixtures import FillPolicy

pytestmark = pytest.mark.integration

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
MARKS = {"SPY": Decimal("600.00")}


@dataclass
class Harness:
    """A fully wired execution stack, offline and deterministic."""

    service: ExecutionService
    gateway: RiskGateway
    ledger: PortfolioLedger
    broker: DeterministicFakeBroker
    projection: OrderProjection
    emitter: EventEmitter
    journal: MemoryJournal

    def create_intent(
        self, *, intent_id: str = "bt-oi_1", quantity: int = 8, side: OrderSide = OrderSide.BUY
    ) -> OrderIntentView:
        view = OrderIntentView(
            intent_id=intent_id,
            decision_id="bt-dec_1",
            instrument="SPY",
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            client_order_id=f"TQ-bt-{intent_id[-1]}",
        )
        self.emitter.emit(
            OrderIntentCreated,
            occurred_at=AT,
            intent_id=view.intent_id,
            decision_id=view.decision_id,
            instrument=view.instrument,
            side=view.side,
            quantity=view.quantity,
            order_type=view.order_type,
            time_in_force=view.time_in_force,
            limit_price=None,
            client_order_id=view.client_order_id,
        )
        return view

    def approve(self, view: OrderIntentView) -> str:
        request = RiskRequest(
            order=view,
            marks=dict(MARKS),
            mark_available_at={"SPY": AT},
            as_of=AT,
            startup_gate_open=True,
            orders_this_cycle=0,
            snapshot_usable=True,
            session=TradingSession(),
            submission_target=SubmissionTarget.SIMULATION,
            mode=Mode.BACKTEST,
        )
        outcome = self.gateway.evaluate(request, portfolio=self.ledger)
        assert outcome.allowed, outcome.detail
        return self.gateway.issue(
            outcome,
            order=view,
            portfolio_digest=outcome.portfolio_digest,
            approved_at=AT,
            risk_event_id="ev-risk",
        ).token


def build_harness(
    *,
    fill_policy: FillPolicy = "immediate",
    gate_open: bool = True,
    submission_target: SubmissionTarget = SubmissionTarget.SIMULATION,
    mode: Mode = Mode.BACKTEST,
    partial_fraction: str = "0.5",
    policy: RiskPolicy,
) -> Harness:
    clock = SimulatedClock(AT)
    ids = IdFactory(capabilities_for(mode).namespace, clock)
    journal = MemoryJournal(mode)
    projection = OrderProjection()
    emitter = EventEmitter(
        mode=mode,
        sink=journal,
        ids=ids,
        clock=clock,
        correlation_id="test",
        observers=(projection.apply,),
    )
    ledger = PortfolioLedger(initial_cash=Decimal("100000.00"))
    broker = DeterministicFakeBroker(
        BrokerFixture(
            fill_policy=fill_policy,
            partial_fill_fraction=Decimal(partial_fraction),
            commission_per_share=Decimal("0.005"),
        ),
        mark_provider=MARKS.get,
    )
    registry = ApprovalRegistry()
    gateway = RiskGateway(policy=policy, state=RiskState(), registry=registry)
    gate = StartupGate()
    if gate_open:
        gate.open(reason="test")
    service = ExecutionService(
        broker=broker,
        gateway=gateway,
        emitter=emitter,
        ledger=ledger,
        projection=projection,
        gate=gate,
        ids=ids,
        mode_caps=capabilities_for(mode),
        submission_target=submission_target,
    )
    return Harness(
        service=service,
        gateway=gateway,
        ledger=ledger,
        broker=broker,
        projection=projection,
        emitter=emitter,
        journal=journal,
    )


class TestApprovalDiscipline:
    def test_submitting_without_an_approval_is_impossible(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy)
        with pytest.raises(RiskDenied, match="not outstanding"):
            harness.service.submit("apr:forged", at=AT)

    def test_an_approval_can_only_be_used_once(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy)
        view = harness.create_intent()
        token = harness.approve(view)
        harness.service.submit(token, at=AT)
        with pytest.raises(RiskDenied, match="already consumed"):
            harness.service.submit(token, at=AT)

    def test_mode_with_no_submission_target_refuses(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, submission_target=SubmissionTarget.NONE)
        view = harness.create_intent()
        token = harness.approve(view)
        with pytest.raises(ModeViolation):
            harness.service.submit(token, at=AT)


class TestStartupGate:
    def test_closed_gate_blocks_submission(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, gate_open=False)
        view = harness.create_intent()
        token = harness.approve(view)
        with pytest.raises(RiskDenied, match="startup gate is closed"):
            harness.service.submit(token, at=AT)

    def test_startup_reconciliation_opens_the_gate(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, gate_open=False)
        assert not harness.service.gate.is_open
        report = harness.service.startup_reconcile(at=AT)
        assert report.gate_opened
        assert harness.service.gate.is_open


class TestFillHandling:
    def test_full_fill_books_the_position(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy)
        view = harness.create_intent()
        token = harness.approve(view)
        result = harness.service.submit(token, at=AT)
        assert result.accepted
        assert result.state is BrokerOrderState.FILLED
        assert harness.ledger.position("SPY").quantity == 8
        assert harness.ledger.cash < Decimal("100000.00")

    def test_duplicate_reconciliation_does_not_double_book(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy)
        view = harness.create_intent()
        harness.service.submit(harness.approve(view), at=AT)
        booked = harness.ledger.applied_fill_count
        harness.service.reconcile(at=AT)
        harness.service.reconcile(at=AT)
        assert harness.ledger.applied_fill_count == booked
        assert harness.ledger.position("SPY").quantity == 8

    def test_partial_fill_then_cancel_reconciles_exactly(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, fill_policy="partial")
        view = harness.create_intent(quantity=8)
        result = harness.service.submit(harness.approve(view), at=AT)
        assert result.state is BrokerOrderState.PARTIALLY_FILLED
        assert result.fills[0].quantity == 4

        canceled = harness.service.cancel(view.intent_id, at=AT, reason_code=ReasonCode.RISK_HALTED)
        assert canceled == 4

        snapshot = harness.projection.get(view.intent_id)
        assert snapshot is not None
        assert snapshot.status.value == "CANCELED"
        assert snapshot.filled_quantity == 4
        assert snapshot.remaining_quantity == 4
        # Ledger inventory and realized amounts must agree with the venue.
        assert harness.ledger.position("SPY").quantity == harness.broker.broker_positions()["SPY"]
        venue = harness.broker.query(client_order_id=view.client_order_id)
        assert venue is not None
        assert venue.filled_quantity == harness.ledger.position("SPY").quantity

    def test_rejected_order_books_nothing(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, fill_policy="reject")
        view = harness.create_intent()
        result = harness.service.submit(harness.approve(view), at=AT)
        assert not result.accepted
        assert result.state is BrokerOrderState.REJECTED
        assert harness.ledger.position("SPY").quantity == 0
        assert harness.ledger.cash == Decimal("100000.00")

    def test_partial_fill_blocks_further_exposure(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, fill_policy="partial")
        view = harness.create_intent(quantity=8)
        harness.service.submit(harness.approve(view), at=AT)
        blocking = harness.projection.blocking_intents()
        assert len(blocking) == 1
        assert blocking[0].blocks_new_exposure


class TestUnknownOutcome:
    def test_timeout_leaves_submit_unknown_and_is_never_retried(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, fill_policy="timeout")
        view = harness.create_intent()
        result = harness.service.submit(harness.approve(view), at=AT)
        assert result.state is BrokerOrderState.UNKNOWN
        snapshot = harness.projection.get(view.intent_id)
        assert snapshot is not None
        assert snapshot.status.value == "SUBMIT_UNKNOWN"
        assert snapshot.attempts == 1

    def test_reconciliation_finds_the_order_the_venue_received(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, fill_policy="timeout")
        view = harness.create_intent()
        harness.service.submit(harness.approve(view), at=AT)
        report = harness.service.reconcile(at=AT)
        assert view.intent_id in report.resolved
        snapshot = harness.projection.get(view.intent_id)
        assert snapshot is not None
        assert snapshot.status.value == "ACKNOWLEDGED"
        assert snapshot.attempts == 1  # still exactly one attempt

    def test_unresolvable_unknown_outcome_is_quarantined(self, policy: RiskPolicy) -> None:
        harness = build_harness(policy=policy, fill_policy="timeout")
        view = harness.create_intent()
        harness.service.submit(harness.approve(view), at=AT)
        # Simulate the venue losing all record of the order.
        harness.broker._orders_by_client_id.clear()
        harness.broker._orders_by_broker_id.clear()
        report = harness.service.reconcile(at=AT)
        assert view.intent_id in report.quarantined
        snapshot = harness.projection.get(view.intent_id)
        assert snapshot is not None
        assert snapshot.status.value == "QUARANTINED"
        assert snapshot.blocks_new_exposure

    def test_reconcile_does_not_open_the_gate_when_something_is_stuck(
        self, policy: RiskPolicy
    ) -> None:
        harness = build_harness(policy=policy, fill_policy="partial", gate_open=False)
        view = harness.create_intent(quantity=8)
        harness.service.gate.open(reason="forced for the test")
        harness.service.submit(harness.approve(view), at=AT)
        harness.service.gate.close(reason="simulating a restart")
        report = harness.service.startup_reconcile(at=AT)
        assert not report.gate_opened
        assert not harness.service.gate.is_open
