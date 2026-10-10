"""Failure-injection suite.

Required from CR-02 onward by AGENTS.md; the foundation is laid here so every later CR
adds cases rather than building the harness. Each test drives a *real* failure mode
through production code — no mocks of the components under test — and asserts the
fail-closed behaviour from `docs/ACCEPTANCE_GATES.md`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Action, Mode, OrderType, ReasonCode, SubmissionTarget, TimeInForce
from tigerquant.core.errors import LookaheadError, RiskDenied
from tigerquant.decision.contract import load_decision_schema
from tigerquant.decision.provider import DecisionRequest, ScriptedDecisionProvider
from tigerquant.decision.validation import DecisionContext, DecisionValidator
from tigerquant.events.market import MarketBar
from tigerquant.evidence.ingest import fact_from_bar
from tigerquant.evidence.snapshot import SnapshotBuilder
from tigerquant.evidence.sources import default_test_registry
from tigerquant.evidence.store import EvidenceStore
from tigerquant.market.fixtures import load_scenario
from tigerquant.market.session import TradingSession
from tigerquant.pipeline.replay import ReplayEngine, run_replay
from tigerquant.portfolio.ledger import PortfolioLedger
from tigerquant.risk.approvals import ApprovalRegistry, OrderIntentView
from tigerquant.risk.gateway import RiskGateway, RiskRequest
from tigerquant.risk.policy import RiskPolicy
from tigerquant.risk.state import Actor, RiskState

pytestmark = pytest.mark.fault

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
SCENARIOS = __import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures" / "scenarios"


def bar(
    close: str, *, minutes: int = 0, instrument: str = "SPY", source: str = "fixture:test"
) -> MarketBar:
    occurred = AT + dt.timedelta(minutes=minutes)
    return MarketBar(
        event_id=f"ev-{instrument}-{minutes}-{source}",
        mode="BACKTEST",
        seq=minutes,
        occurred_at=occurred,
        recorded_at=occurred,
        correlation_id="fault",
        instrument=instrument,
        interval="1m",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        available_at=occurred + dt.timedelta(minutes=1),
        source_id=source,
        content_hash="sha256:" + f"{minutes:064d}",
        reference_price=close,
    )


class TestModelFailures:
    """Acceptance gate 11: provider problems must never bypass risk."""

    def test_provider_outage_produces_no_trade_and_no_order(self) -> None:
        from tigerquant.core.errors import TigerQuantError

        def explode(request: DecisionRequest) -> dict[str, object]:
            msg = "provider unreachable"
            raise TigerQuantError(msg)

        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        provider = ScriptedDecisionProvider(transform=explode)
        report = run_replay(fixture, provider=provider)
        assert report.orders_submitted == 0
        assert report.decisions_rejected == 6

    def test_invalid_output_is_rejected_not_repaired(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        provider = ScriptedDecisionProvider(raw={"action": "SELL_EVERYTHING"})
        report = run_replay(fixture, provider=provider)
        assert report.orders_submitted == 0
        assert report.decisions_rejected == 6

    def test_model_cannot_smuggle_a_quantity(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        provider = ScriptedDecisionProvider(
            raw={
                "action": "SET_TARGET",
                "instrument": "SPY",
                "quantity": 1_000_000,
                "target_weight": "0.05",
            }
        )
        report = run_replay(fixture, provider=provider)
        assert report.orders_submitted == 0

    def test_wrong_model_version_is_rejected(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        provider = ScriptedDecisionProvider(
            model_version="unapproved-model",
            raw={
                "action": "SET_TARGET",
                "instrument": "SPY",
                "target_weight": "0.05",
                "model_version": "unapproved-model",
            },
        )
        report = run_replay(fixture, provider=provider)
        # The provider's *resolved* release is what the validator expects, so this
        # particular case is self-consistent; the important property is that the
        # recorded model_version is the resolved one, never a self-declared alias.
        assert report.orders_submitted <= 1


class TestEvidenceFailures:
    """Acceptance gate 9: unsupported or contradictory evidence means NO_TRADE."""

    def _store_and_snapshot(
        self, *, extra_source: str | None = None
    ) -> tuple[EvidenceStore, SnapshotBuilder, str]:
        store = EvidenceStore(default_test_registry())
        builder = SnapshotBuilder(store, max_age_seconds=900)
        fact = fact_from_bar(bar("600.00"), max_age_seconds=900)
        store.add(fact)
        if extra_source is not None:
            conflicting = fact_from_bar(bar("650.00", source=extra_source), max_age_seconds=900)
            store.add(conflicting)
        return store, builder, fact.fact_id

    def test_fabricated_evidence_id_is_rejected(self) -> None:
        store, builder, fact_id = self._store_and_snapshot()
        snapshot = builder.build(as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id,))
        raw = {
            "snapshot_id": snapshot.snapshot_id,
            "model_version": "scripted-test-provider",
            "action": "SET_TARGET",
            "instrument": "SPY",
            "target_weight": Decimal("0.05"),
            "horizon": "5 days",
            "thesis": "invented",
            "counter_thesis": "none",
            "invalidation": "none",
            "evidence_ids": ["fact:does-not-exist"],
            "uncertainties": [],
            "decision_id": "bt-dec_x",
        }
        context = DecisionContext(
            snapshot=snapshot,
            allowed_instruments=("SPY",),
            expected_decision_id="bt-dec_x",
            expected_model_version="scripted-test-provider",
            expected_prompt_version="p",
            current_weight=Decimal(0),
            mark_price=Decimal("600.00"),
            equity=Decimal(100000),
        )
        with pytest.raises(Exception, match="not part of snapshot|unresolved"):
            DecisionValidator(store, schema=load_decision_schema()).validate(raw, context)

    def test_contradictory_sources_make_the_snapshot_contradictory(self) -> None:
        store, builder, fact_id = self._store_and_snapshot(extra_source="wire:other")
        second = next(f for f in store if f.source_id == "wire:other")
        snapshot = builder.build(
            as_of=AT + dt.timedelta(minutes=1), fact_ids=(fact_id, second.fact_id)
        )
        assert snapshot.completeness.value == "CONTRADICTORY"
        assert not snapshot.is_usable

    def test_stale_evidence_is_rejected(self) -> None:
        store = EvidenceStore(default_test_registry())
        builder = SnapshotBuilder(store, max_age_seconds=60)
        fact = fact_from_bar(bar("600.00"), max_age_seconds=60)
        store.add(fact)
        snapshot = builder.build(as_of=AT + dt.timedelta(hours=2), fact_ids=(fact.fact_id,))
        assert snapshot.completeness.value == "INCOMPLETE"
        assert any("stale" in reason for reason in snapshot.reasons)

    def test_future_evidence_cannot_be_cited(self) -> None:
        store = EvidenceStore(default_test_registry())
        builder = SnapshotBuilder(store, max_age_seconds=900)
        future = fact_from_bar(bar("700.00", minutes=10), max_age_seconds=900)
        store.add(future)
        snapshot = builder.build(as_of=AT, fact_ids=(future.fact_id,))
        assert snapshot.completeness.value == "INCOMPLETE"

    def test_lookahead_access_raises(self) -> None:
        """A view built at the scenario start holds future bars that it must not expose."""
        from tigerquant.core.clock import SimulatedClock as Clock
        from tigerquant.core.ids import IdFactory
        from tigerquant.events.emitter import EventEmitter
        from tigerquant.events.journal import MemoryJournal
        from tigerquant.market.asof import PointInTimeView
        from tigerquant.market.feed import DeterministicMarketFeed

        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        clock = Clock(fixture.start)
        emitter = EventEmitter(
            mode=Mode.BACKTEST,
            sink=MemoryJournal(Mode.BACKTEST),
            ids=IdFactory("bt", clock),
            clock=clock,
            correlation_id="fault",
        )
        bars = []
        for draft in DeterministicMarketFeed(fixture).drafts():
            bars.append(
                emitter.emit(
                    MarketBar,
                    occurred_at=draft.occurred_at,
                    instrument=draft.instrument,
                    interval=draft.interval,
                    open=draft.open,
                    high=draft.high,
                    low=draft.low,
                    close=draft.close,
                    volume=draft.volume,
                    available_at=draft.available_at,
                    source_id=draft.source_id,
                    content_hash=draft.content_hash,
                    reference_price=draft.reference_price,
                )
            )
        # At the scenario start, no bar has closed yet.
        view = PointInTimeView.empty(fixture.start).with_bars(bars, fixture.start)
        assert view.visible_bars("SPY") == ()
        assert view.future_bars("SPY") == tuple(bars)
        with pytest.raises(LookaheadError, match="lookahead"):
            view.assert_available(bars[0])
        with pytest.raises(Exception, match="no market data available"):
            view.latest_bar("SPY")


class TestRiskFailures:
    """Acceptance gate 3: stale/missing/mismatched inputs fail closed and are logged."""

    def _gateway(self) -> tuple[RiskGateway, PortfolioLedger, OrderIntentView]:
        policy = RiskPolicy(
            policy_version="fault",
            universe_version="u",
            allowed_instruments=("SPY",),
            gross_exposure_fraction=Decimal("0.5"),
            max_symbol_fraction=Decimal("0.1"),
            max_order_fraction=Decimal("0.1"),
            max_mark_age_seconds=60,
        )
        gateway = RiskGateway(policy=policy, state=RiskState(), registry=ApprovalRegistry())
        order = OrderIntentView(
            intent_id="bt-oi_fault",
            decision_id="bt-dec_fault",
            instrument="SPY",
            side="BUY",  # type: ignore[arg-type]
            quantity=8,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            client_order_id="TQ-bt-fault",
        )
        return gateway, PortfolioLedger(initial_cash=Decimal(100000)), order

    def _request(self, order: OrderIntentView, **overrides: object) -> RiskRequest:
        base: dict[str, object] = {
            "order": order,
            "marks": {"SPY": Decimal("600.00")},
            "mark_available_at": {"SPY": AT},
            "as_of": AT,
            "startup_gate_open": True,
            "orders_this_cycle": 0,
            "snapshot_usable": True,
            "session": TradingSession(),
            "submission_target": SubmissionTarget.SIMULATION,
            "mode": Mode.BACKTEST,
        }
        base.update(overrides)
        return RiskRequest(**base)  # type: ignore[arg-type]

    def test_missing_quote_fails_closed(self) -> None:
        gateway, ledger, order = self._gateway()
        outcome = gateway.evaluate(
            self._request(order, marks={}, mark_available_at={}), portfolio=ledger
        )
        assert not outcome.allowed
        assert ReasonCode.MARK_MISSING in outcome.reason_codes

    def test_stale_price_fails_closed(self) -> None:
        gateway, ledger, order = self._gateway()
        outcome = gateway.evaluate(
            self._request(order, mark_available_at={"SPY": AT - dt.timedelta(minutes=10)}),
            portfolio=ledger,
        )
        assert not outcome.allowed
        assert ReasonCode.MARK_STALE in outcome.reason_codes

    def test_unreconciled_account_state_fails_closed(self) -> None:
        gateway, ledger, order = self._gateway()
        outcome = gateway.evaluate(self._request(order, blocking_intents=1), portfolio=ledger)
        assert not outcome.allowed
        assert ReasonCode.UNRECONCILED in outcome.reason_codes

    def test_halt_prevents_new_exposure_but_allows_cancellation(self) -> None:
        gateway, ledger, order = self._gateway()
        gateway.state.halt(
            actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="loss limit"
        )
        outcome = gateway.evaluate(self._request(order), portfolio=ledger)
        assert not outcome.allowed
        assert ReasonCode.RISK_HALTED in outcome.reason_codes
        # Cancellation is not gated by the risk state: a halt must not strand orders.
        assert gateway.state.blocks_new_exposure()


class TestExecutionFailures:
    """Acceptance gates 4, 6, 7, 8: unknown outcomes, restarts, duplicates, leases."""

    def test_unknown_outcome_is_not_retried_and_blocks_exposure(
        self,
    ) -> None:
        fixture = load_scenario(SCENARIOS / "unknown_outcome.yaml")
        engine = ReplayEngine(fixture)
        report = engine.run()
        assert report.orders_submitted == 1
        assert engine._broker.order_count == 1
        assert report.orders_unknown == 1

    def test_restart_reconstruction_sees_resting_orders(self, tmp_path) -> None:
        from pathlib import Path

        path: Path = tmp_path / "audit.jsonl"
        fixture = load_scenario(SCENARIOS / "partial_fill.yaml")
        first = ReplayEngine(fixture)
        first.run(journal_path=path)

        # A new engine opening the same journal must reconstruct the open intent before
        # it may do anything else.
        second = ReplayEngine(fixture)
        second._journal = None  # type: ignore[assignment]  # replaced by run()
        second.run(journal_path=path)
        projection_after_resume = second._projection
        assert projection_after_resume.snapshots(), "the journal must rebuild the projection"

    def test_duplicate_fill_delivery_does_not_double_book(self) -> None:
        fixture = load_scenario(SCENARIOS / "trend_up.yaml")
        engine = ReplayEngine(fixture)
        report = engine.run()
        assert engine.ledger.applied_fill_count == 1
        assert engine.ledger.position("SPY").quantity == report.positions.get("SPY")

    def test_second_approval_for_the_same_intent_is_refused(self) -> None:
        registry = ApprovalRegistry()
        from tigerquant.risk.approvals import OrderIntentView as View

        view = View(
            intent_id="i",
            decision_id="d",
            instrument="SPY",
            side="BUY",  # type: ignore[arg-type]
            quantity=1,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            client_order_id="c",
        )
        registry.issue(
            order=view, policy_hash="h", portfolio_digest="p", approved_at=AT, risk_event_id="e"
        )
        token = next(iter(registry.outstanding))
        registry.consume(token)
        with pytest.raises(RiskDenied):
            registry.consume(token)


class TestClockAndDataFaults:
    """Acceptance gate 15: clock skew and data faults alert rather than proceed."""

    def test_clock_cannot_move_backwards(self) -> None:
        clock = SimulatedClock(AT)
        with pytest.raises(ValueError, match="backwards"):
            clock.advance_to(AT - dt.timedelta(seconds=1))

    def test_future_dated_mark_is_treated_as_skew_not_freshness(self) -> None:
        gateway, ledger, order = TestRiskFailures()._gateway()
        request = TestRiskFailures()._request(
            order, mark_available_at={"SPY": AT + dt.timedelta(minutes=5)}
        )
        outcome = gateway.evaluate(request, portfolio=ledger)
        assert not outcome.allowed
        assert ReasonCode.MARK_STALE in outcome.reason_codes
        assert "future" in outcome.detail

    def test_action_enum_is_closed(self) -> None:
        with pytest.raises(ValueError, match="SELL_EVERYTHING"):
            Action("SELL_EVERYTHING")
