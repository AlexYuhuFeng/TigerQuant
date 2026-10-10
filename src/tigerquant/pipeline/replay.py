"""End-to-end synthetic replay — the CR-01 vertical slice.

One call takes a scenario fixture and drives the complete production path: deterministic
market feed → as-of view → evidence → snapshot → decision provider → schema and evidence
validation → deterministic sizing → risk gateway → order intent → broker transport →
ledger → append-only audit journal.

Determinism is a hard requirement, not a nicety: two runs of the same fixture in fresh
interpreters must produce byte-identical journals (ADR-0004 §5). Everything that could
vary — time, identifiers, iteration order — is injected (ADR-0011).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import (
    Action,
    OrderSide,
    OrderType,
    ReasonCode,
    Severity,
    TimeInForce,
    capabilities_for,
    resolve_submission_target,
)
from tigerquant.core.errors import DecisionValidationError, RiskDenied, TigerQuantError
from tigerquant.core.ids import IdFactory, stable_token
from tigerquant.core.money import ZERO, q_cash
from tigerquant.decision.contract import load_decision_schema
from tigerquant.decision.provider import DecisionRequest, StrategyDecisionProvider
from tigerquant.decision.validation import DecisionContext, DecisionValidator
from tigerquant.events.decision import DecisionProposed, DecisionRejected, SnapshotCreated
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.execution import OrderIntentCreated
from tigerquant.events.journal import AppendOnlyJournal, MemoryJournal
from tigerquant.events.market import MarketBar
from tigerquant.events.portfolio import LedgerMarked
from tigerquant.events.risk import AlertRaised, RiskEvaluated, RiskStateChanged
from tigerquant.evidence.ingest import fact_from_bar
from tigerquant.evidence.snapshot import SnapshotBuilder
from tigerquant.evidence.store import EvidenceStore
from tigerquant.execution.broker import BrokerOrderState
from tigerquant.execution.fake_broker import DeterministicFakeBroker
from tigerquant.execution.intent import OrderProjection
from tigerquant.execution.service import ExecutionService, StartupGate, make_mark_provider
from tigerquant.market.asof import PointInTimeView
from tigerquant.market.feed import DeterministicMarketFeed
from tigerquant.market.fixtures import ScenarioFixture
from tigerquant.market.session import TradingSession
from tigerquant.portfolio.ledger import PortfolioLedger
from tigerquant.portfolio.sizing import order_quantity, shares_for_weight
from tigerquant.risk.approvals import ApprovalRegistry, OrderIntentView
from tigerquant.risk.gateway import RiskGateway, RiskRequest
from tigerquant.risk.policy import RiskPolicy
from tigerquant.risk.state import Actor, RiskState
from tigerquant.strategy.reference import REFERENCE_MODEL_ID, MomentumReferenceStrategy

if TYPE_CHECKING:
    from pathlib import Path

    from tigerquant.decision.provider import DecisionProvider
    from tigerquant.evidence.snapshot import EvidenceSnapshot
    from tigerquant.market.feed import BarDraft

__all__ = ["ReplayEngine", "ReplayReport", "build_policy_from_fixture", "run_replay"]

PROMPT_VERSION = "reference-rules-v1"
POLICY_VERSION = "cr01-baseline"


def build_policy_from_fixture(
    fixture: ScenarioFixture, *, version: str = POLICY_VERSION
) -> RiskPolicy:
    """Translate fixture risk parameters into the immutable policy object."""
    return RiskPolicy(
        policy_version=version,
        universe_version=f"{fixture.scenario_id}:v1",
        allowed_instruments=tuple(fixture.universe),
        gross_exposure_fraction=fixture.risk.gross_exposure_fraction,
        max_symbol_fraction=fixture.risk.max_symbol_fraction,
        max_order_fraction=fixture.risk.max_order_fraction,
        max_mark_age_seconds=fixture.risk.max_mark_age_seconds,
        allow_margin=fixture.risk.allow_margin,
        allow_short=fixture.risk.allow_short,
        allow_options=fixture.risk.allow_options,
        max_orders_per_cycle=fixture.risk.max_orders_per_cycle,
    )


class ReplayReport(BaseModel):
    """Machine-checkable summary of one replay, recorded as evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    mode: str
    implementation_status: str
    fixture_digest: str
    bars_processed: int
    decisions_proposed: int
    decisions_rejected: int
    risk_denials: int
    orders_submitted: int
    orders_filled: int
    orders_rejected: int
    orders_unknown: int
    orders_quarantined: int
    initial_cash: Decimal
    final_cash: Decimal
    final_equity: Decimal
    realized_pnl: Decimal
    positions: dict[str, int]
    journal_records: int
    journal_head_hash: str
    journal_digest: str
    snapshot_digest: str
    strategies: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class _Counters:
    bars: int = 0
    proposed: int = 0
    rejected: int = 0
    risk_denials: int = 0
    submitted: int = 0
    filled: int = 0
    rejected_orders: int = 0
    unknown: int = 0
    quarantined: int = 0
    alerts: list[str] = field(default_factory=list)


class ReplayEngine:
    """Drives the full slice for one scenario."""

    def __init__(
        self,
        fixture: ScenarioFixture,
        *,
        provider: DecisionProvider | None = None,
        session: TradingSession | None = None,
        schema: dict[str, Any] | None = None,
    ) -> None:
        self._fixture = fixture
        self._mode = fixture.mode
        self._caps = capabilities_for(fixture.mode)
        self._clock = SimulatedClock(fixture.start)
        self._ids = IdFactory(self._caps.namespace, self._clock)
        self._session = session or TradingSession()
        self._store = EvidenceStore()
        self._builder = SnapshotBuilder(self._store, max_age_seconds=fixture.max_fact_age_seconds)
        self._ledger = PortfolioLedger(initial_cash=fixture.initial_cash)
        self._projection = OrderProjection()
        self._gate = StartupGate()
        self._registry = ApprovalRegistry()
        self._state = RiskState(halted=fixture.risk.halted)
        self._policy = build_policy_from_fixture(fixture)
        self._gateway = RiskGateway(policy=self._policy, state=self._state, registry=self._registry)
        self._schema = schema if schema is not None else load_decision_schema()
        self._validator = DecisionValidator(self._store, schema=self._schema)
        self._feed = DeterministicMarketFeed(fixture)
        self._marks: dict[str, Decimal] = {}
        self._mark_times: dict[str, dt.datetime] = {}
        self._fact_ids: list[str] = []
        self._view = PointInTimeView.empty(fixture.start)
        self._strategy = MomentumReferenceStrategy(fixture.strategy)
        self._provider: DecisionProvider = provider or StrategyDecisionProvider(
            self._strategy, model_version=REFERENCE_MODEL_ID, prompt_version=PROMPT_VERSION
        )
        self._broker = DeterministicFakeBroker(
            fixture.broker, mark_provider=make_mark_provider(self._marks)
        )
        self._submission_target = resolve_submission_target(
            fixture.mode, self._broker.implementation_status
        )
        self._journal: AppendOnlyJournal | MemoryJournal = MemoryJournal(fixture.mode)
        self._emitter = EventEmitter(
            mode=fixture.mode,
            sink=self._journal,
            ids=self._ids,
            clock=self._clock,
            correlation_id=f"{fixture.scenario_id}-init",
            observers=(self._projection.apply,),
        )
        self._service = ExecutionService(
            broker=self._broker,
            gateway=self._gateway,
            emitter=self._emitter,
            ledger=self._ledger,
            projection=self._projection,
            gate=self._gate,
            ids=self._ids,
            mode_caps=self._caps,
            submission_target=self._submission_target,
        )
        self._counters = _Counters()
        self._orders_this_cycle = 0
        self._last_snapshot_digest = ""

    # -- public ------------------------------------------------------------------

    @property
    def journal(self) -> AppendOnlyJournal | MemoryJournal:
        return self._journal

    @property
    def ledger(self) -> PortfolioLedger:
        return self._ledger

    @property
    def counters(self) -> _Counters:
        return self._counters

    def run(self, *, journal_path: Path | None = None) -> ReplayReport:
        """Run the scenario to completion and return the evidence report."""
        if journal_path is not None:
            self._journal = AppendOnlyJournal(journal_path, self._mode).open()
            # Startup reconstruction: rebuilding the order projection from the existing
            # journal is what lets a restarted process know what is already resting
            # (ADR-0008 §4).
            for record in self._journal.iter_records():
                self._projection.apply(record.event)
            self._emitter = EventEmitter(
                mode=self._mode,
                sink=self._journal,
                ids=self._ids,
                clock=self._clock,
                correlation_id=f"{self._fixture.scenario_id}-init",
                observers=(self._projection.apply,),
            )
            self._rebind_emitter()

        try:
            self._announce_start()
            # Nothing may be submitted until resting state is accounted for.
            report = self._service.startup_reconcile(at=self._clock.now())
            if not report.gate_opened:
                self._emit_alert(
                    ReasonCode.STARTUP_GATE_CLOSED,
                    "startup reconciliation did not open the gate; no orders will be sent",
                )
            for step in self._feed.schedule():
                draft = self._feed.draft(step)
                self._clock.advance_to(draft.available_at)
                self._process_bar(draft, step_index=step.index)
            return self._finalise()
        finally:
            if isinstance(self._journal, AppendOnlyJournal):
                self._journal.close()

    # -- steps -------------------------------------------------------------------

    def _rebind_emitter(self) -> None:
        self._service = ExecutionService(
            broker=self._broker,
            gateway=self._gateway,
            emitter=self._emitter,
            ledger=self._ledger,
            projection=self._projection,
            gate=self._gate,
            ids=self._ids,
            mode_caps=self._caps,
        )

    def _announce_start(self) -> None:
        self._emitter.emit(
            RiskStateChanged,
            halted=self._state.halted,
            frozen=self._state.frozen,
            reason_code=ReasonCode.RISK_HALTED,
            detail=f"replay start; policy {self._policy.policy_version}",
            actor=Actor.RISK_ENGINE.value,
            occurred_at=self._clock.now(),
        )

    def _process_bar(self, draft: BarDraft, *, step_index: int) -> None:
        correlation_id = f"{self._fixture.scenario_id}-{step_index:04d}"
        bar = self._emitter.emit(
            MarketBar,
            correlation_id=correlation_id,
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
        self._counters.bars += 1

        fact = fact_from_bar(bar, max_age_seconds=self._fixture.max_fact_age_seconds)
        if self._store.add(fact):
            self._fact_ids.append(fact.fact_id)

        self._marks[draft.instrument] = bar.close
        self._mark_times[draft.instrument] = bar.available_at
        self._view = self._view.with_bars((bar,), self._clock.now())

        self._decision_cycle(correlation_id=correlation_id)
        self._poll_broker(correlation_id=correlation_id)
        self._mark_ledger(correlation_id=correlation_id)

    def _decision_cycle(self, *, correlation_id: str) -> None:
        as_of = self._clock.now()
        self._orders_this_cycle = 0
        snapshot = self._build_snapshot(as_of=as_of, correlation_id=correlation_id)
        self._last_snapshot_digest = snapshot.digest

        equity = self._safe_equity()
        decision_id = self._ids.next_id("dec")
        request = DecisionRequest(
            snapshot=snapshot,
            as_of=as_of,
            decision_id=decision_id,
            model_version=self._provider.model_version,
            prompt_version=self._provider.prompt_version,
            universe=tuple(self._fixture.universe),
            equity=equity,
            marks=dict(self._marks),
            current_weights={
                symbol: self._ledger.weight_of(symbol, self._marks)
                for symbol in self._fixture.universe
            },
            view=self._view,
        )

        try:
            raw = self._provider.propose(request)
        except TigerQuantError as exc:
            self._reject_decision(
                decision_id=decision_id,
                snapshot=snapshot,
                stage="provider",
                reason_code=ReasonCode.MODEL_UNAVAILABLE,
                detail=str(exc),
                correlation_id=correlation_id,
            )
            return

        context = DecisionContext(
            snapshot=snapshot,
            allowed_instruments=tuple(self._fixture.universe),
            expected_decision_id=decision_id,
            expected_model_version=self._provider.model_version,
            expected_prompt_version=self._provider.prompt_version,
            current_weight=self._current_weight_for(raw),
            mark_price=self._mark_for_raw(raw),
            equity=equity,
        )

        try:
            decision = self._validator.validate(raw, context)
        except DecisionValidationError as exc:
            self._reject_decision(
                decision_id=decision_id,
                snapshot=snapshot,
                stage=exc.stage,
                reason_code=exc.reason_code,
                detail=str(exc),
                correlation_id=correlation_id,
            )
            return

        self._emitter.emit(
            DecisionProposed,
            correlation_id=correlation_id,
            occurred_at=as_of,
            decision_id=decision.decision_id,
            snapshot_id=decision.snapshot_id,
            model_version=decision.model_version,
            prompt_version=request.prompt_version,
            action=decision.action,
            instrument=decision.instrument,
            target_weight=decision.target_weight,
            horizon=decision.horizon,
            thesis=decision.thesis,
            counter_thesis=decision.counter_thesis,
            invalidation=decision.invalidation,
            evidence_ids=decision.evidence_ids,
            uncertainties=decision.uncertainties,
            recomputed_notional=(
                context.equity * decision.target_weight
                if (
                    context.equity is not None
                    and decision.target_weight is not None
                    and decision.target_weight > ZERO
                )
                else None
            ),
        )
        self._counters.proposed += 1

        if decision.action in {Action.NO_TRADE, Action.HOLD}:
            return
        self._route_to_execution(
            decision_id=decision.decision_id,
            action=decision.action,
            instrument=decision.instrument,
            target_weight=decision.target_weight,
            snapshot=snapshot,
            correlation_id=correlation_id,
        )

    def _route_to_execution(
        self,
        *,
        decision_id: str,
        action: Action,
        instrument: str | None,
        target_weight: Decimal | None,
        snapshot: EvidenceSnapshot,
        correlation_id: str,
    ) -> None:
        if instrument is None or target_weight is None:
            return
        # One in-flight order at a time. A second intent for a still-open order would be
        # a duplicate exposure request, and an unresolved order must never be joined by
        # a fresh one (ADR-0008 §3).
        if any(snapshot_open.is_open for snapshot_open in self._projection.snapshots()):
            return
        mark = self._marks.get(instrument)
        if mark is None or mark <= ZERO:
            self._emit_alert(
                ReasonCode.MARK_MISSING,
                f"no mark for {instrument}; order suppressed",
                correlation_id=correlation_id,
            )
            return

        equity = self._safe_equity()
        if equity <= ZERO:
            self._emit_alert(
                ReasonCode.CHECK_ERROR,
                "equity is not positive; order suppressed",
                correlation_id=correlation_id,
            )
            return

        target_shares = shares_for_weight(target_weight=target_weight, equity=equity, price=mark)
        current_shares = self._ledger.position(instrument).quantity
        if action is Action.CLOSE:
            target_shares = 0
        delta = order_quantity(target_shares=target_shares, current_shares=current_shares)
        if delta == 0:
            return

        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        quantity = abs(delta)
        intent_id = self._ids.next_id("oi")
        client_order_id = f"TQ-{self._caps.namespace}-{stable_token(intent_id, 12)}"

        self._emitter.emit(
            OrderIntentCreated,
            correlation_id=correlation_id,
            occurred_at=self._clock.now(),
            intent_id=intent_id,
            decision_id=decision_id,
            instrument=instrument,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            client_order_id=client_order_id,
        )

        order_view = OrderIntentView(
            intent_id=intent_id,
            decision_id=decision_id,
            instrument=instrument,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            client_order_id=client_order_id,
        )
        risk_request = RiskRequest(
            order=order_view,
            marks=dict(self._marks),
            mark_available_at=dict(self._mark_times),
            as_of=self._clock.now(),
            startup_gate_open=self._gate.is_open,
            orders_this_cycle=self._orders_this_cycle,
            snapshot_usable=snapshot.is_usable,
            session=self._session,
            blocking_intents=len(self._projection.blocking_intents()),
            submission_target=self._submission_target,
            mode=self._mode,
        )
        outcome = self._gateway.evaluate(risk_request, portfolio=self._ledger)
        if not outcome.allowed:
            # Only denials are journaled here; the execution service journals the
            # allowance as the act of submitting it, so it appears exactly once.
            self._emitter.emit(
                RiskEvaluated,
                correlation_id=correlation_id,
                occurred_at=self._clock.now(),
                intent_id=intent_id,
                outcome=outcome.kind,
                reason_codes=outcome.reason_codes,
                policy_hash=outcome.policy_hash,
                portfolio_digest=outcome.portfolio_digest,
                checks_run=outcome.checks_run,
                detail=outcome.detail,
            )
            self._counters.risk_denials += 1
            self._emit_alert(
                outcome.reason_codes[0] if outcome.reason_codes else ReasonCode.CHECK_ERROR,
                f"order intent {intent_id} denied: {outcome.detail}",
                correlation_id=correlation_id,
                severity=Severity.INFO,
            )
            return

        approval = self._gateway.issue(
            outcome,
            order=order_view,
            portfolio_digest=outcome.portfolio_digest,
            approved_at=self._clock.now(),
            risk_event_id=f"deferred:{intent_id}",
        )
        try:
            result = self._service.submit(approval.token, at=self._clock.now())
        except RiskDenied as exc:
            self._emit_alert(
                exc.reason_codes[0], f"submission refused: {exc}", correlation_id=correlation_id
            )
            return
        self._counters.submitted += 1
        self._orders_this_cycle += 1
        if result.state is not None and result.state is BrokerOrderState.FILLED:
            self._counters.filled += 1
        elif result.state is BrokerOrderState.REJECTED:
            self._counters.rejected_orders += 1
        elif result.state is BrokerOrderState.UNKNOWN:
            self._counters.unknown += 1

    def _poll_broker(self, *, correlation_id: str) -> None:
        self._broker.poll(at=self._clock.now())
        # Asynchronous fills and unknown outcomes are resolved by the same reconciliation
        # path used after a restart, so that path runs continuously rather than only once.
        if self._projection.open_intents():
            self._service.reconcile(at=self._clock.now())
        del correlation_id

    def _mark_ledger(self, *, correlation_id: str) -> None:
        try:
            equity = self._ledger.equity(self._marks)
            gross = self._ledger.gross_exposure(self._marks)
            net = self._ledger.net_exposure(self._marks)
        except TigerQuantError:
            return
        self._emitter.emit(
            LedgerMarked,
            correlation_id=correlation_id,
            occurred_at=self._clock.now(),
            as_of=self._clock.now(),
            cash=self._ledger.cash,
            equity=equity,
            gross_exposure=gross,
            net_exposure=net,
            ledger_digest=self._ledger.digest(),
        )

    # -- helpers -----------------------------------------------------------------

    def _build_snapshot(self, *, as_of: dt.datetime, correlation_id: str) -> EvidenceSnapshot:
        snapshot = self._builder.build(as_of=as_of, fact_ids=self._fact_ids)
        self._emitter.emit(
            SnapshotCreated,
            correlation_id=correlation_id,
            occurred_at=as_of,
            snapshot_id=snapshot.snapshot_id,
            as_of=snapshot.as_of,
            fact_ids=snapshot.fact_ids,
            completeness=snapshot.completeness,
            digest=snapshot.digest,
            max_fact_age_seconds=snapshot.max_age_seconds,
            notes=snapshot.notes,
        )
        return snapshot

    def _safe_equity(self) -> Decimal:
        try:
            return self._ledger.equity(self._marks)
        except TigerQuantError:
            return self._ledger.cash

    def _current_weight_for(self, raw: dict[str, Any]) -> Decimal:
        instrument = raw.get("instrument")
        if not isinstance(instrument, str):
            return ZERO
        try:
            return self._ledger.weight_of(instrument, self._marks)
        except TigerQuantError:
            return ZERO

    def _mark_for_raw(self, raw: dict[str, Any]) -> Decimal | None:
        instrument = raw.get("instrument")
        if not isinstance(instrument, str):
            return None
        return self._marks.get(instrument)

    def _reject_decision(
        self,
        *,
        decision_id: str,
        snapshot: EvidenceSnapshot,
        stage: str,
        reason_code: ReasonCode,
        detail: str,
        correlation_id: str,
    ) -> None:
        self._counters.rejected += 1
        self._emitter.emit(
            DecisionRejected,
            correlation_id=correlation_id,
            occurred_at=self._clock.now(),
            decision_id=decision_id,
            snapshot_id=snapshot.snapshot_id,
            stage=stage,
            reason_code=reason_code,
            detail=detail,
        )

    def _emit_alert(
        self,
        code: ReasonCode,
        message: str,
        *,
        correlation_id: str | None = None,
        severity: Severity = Severity.WARNING,
    ) -> None:
        self._counters.alerts.append(f"{severity.value}:{code.value}:{message}")
        self._emitter.emit(
            AlertRaised,
            correlation_id=correlation_id or f"{self._fixture.scenario_id}-alert",
            occurred_at=self._clock.now(),
            severity=severity,
            code=code,
            message=message,
            context={},
        )

    def _finalise(self) -> ReplayReport:
        positions = {
            position.instrument: position.quantity
            for position in self._ledger.positions()
            if not position.is_flat()
        }
        realized = sum((position.realized_pnl for position in self._ledger.positions()), ZERO)
        try:
            equity = self._ledger.equity(self._marks)
        except TigerQuantError:
            equity = self._ledger.cash

        if isinstance(self._journal, AppendOnlyJournal):
            records = list(self._journal.iter_records())
            head = self._journal.head_hash()
            digest = self._journal.digest()
        else:
            records = list(self._journal.records)
            head = records[-1].hash if records else "genesis"
            digest = self._journal.digest()

        return ReplayReport(
            scenario_id=self._fixture.scenario_id,
            mode=self._mode.value,
            implementation_status=self._broker.implementation_status.value,
            fixture_digest=self._fixture.content_digest(),
            bars_processed=self._counters.bars,
            decisions_proposed=self._counters.proposed,
            decisions_rejected=self._counters.rejected,
            risk_denials=self._counters.risk_denials,
            orders_submitted=self._counters.submitted,
            orders_filled=self._counters.filled,
            orders_rejected=self._counters.rejected_orders,
            orders_unknown=self._counters.unknown,
            orders_quarantined=self._counters.quarantined,
            initial_cash=q_cash(self._ledger.initial_cash),
            final_cash=q_cash(self._ledger.cash),
            final_equity=q_cash(equity),
            realized_pnl=q_cash(realized),
            positions=positions,
            journal_records=len(records),
            journal_head_hash=head,
            journal_digest=digest,
            snapshot_digest=self._last_snapshot_digest,
            strategies=(f"{self._strategy.name}@{self._strategy.version}",),
            notes=(
                "SIMULATED: deterministic fake broker; no broker was contacted",
                "policy: " + self._policy.policy_version,
                f"alerts: {len(self._counters.alerts)}",
            ),
        )


def run_replay(
    fixture: ScenarioFixture,
    *,
    journal_path: Path | None = None,
    provider: DecisionProvider | None = None,
    session: TradingSession | None = None,
) -> ReplayReport:
    """Convenience wrapper around :class:`ReplayEngine`."""
    engine = ReplayEngine(fixture, provider=provider, session=session)
    return engine.run(journal_path=journal_path)
