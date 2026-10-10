"""Measured performance: P&L attribution, step latency, and session stability.

CR-09's gate asks for "latency, P&L attribution, stability over representative periods". This
module produces all three from the journal, and — more importantly — labels what each number
actually is.

**The distinction that matters most.** The offline replay drives an injected ``SimulatedClock``
advancing by fixture-defined steps. Durations measured from those timestamps are *step
durations on a simulated clock*. They are **not latency**, they say nothing about how the system
behaves against a real venue, and reporting them as latency would be the single most misleading
thing this file could do. So :class:`LatencyReport` carries a ``clock_source`` and refuses to
present simulated numbers as observed ones: the field is named ``step_duration`` and every
rendering says which clock produced it.

Real latency needs a real paper session, which needs CR-03's write-scoped grant. Until then this
module measures what it can and says plainly what it cannot.

**Attribution is exact, not estimated.** P&L comes from the ledger's own realised figures and
the marks the journal recorded, both ``Decimal``, so the parts sum to the whole. A report whose
components do not add up to the total is worse than no report, so :meth:`AttributionReport.reconciles`
checks that and the tests assert it.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import digest_of
from tigerquant.core.money import ZERO, q_cash
from tigerquant.ops.lineage_report import build_lineage

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tigerquant.events.base import EventBase

__all__ = [
    "AttributionReport",
    "InstrumentAttribution",
    "LatencyReport",
    "SessionMetrics",
    "StepDuration",
    "measure_session",
]

#: The hops we can measure between adjacent journal events, in pipeline order.
_HOPS: tuple[tuple[str, str, str], ...] = (
    ("decision->intent", "DecisionProposed", "OrderIntentCreated"),
    ("intent->submit", "OrderIntentCreated", "OrderSubmitted"),
    ("submit->ack", "OrderSubmitted", "OrderAcknowledged"),
    ("ack->fill", "OrderAcknowledged", "OrderFilled"),
)

#: A clock that is not wall time produces step durations, never latency.
_SIMULATED_CLOCKS = frozenset({"SimulatedClock", "simulated", "fixture"})


@dataclass(frozen=True, slots=True)
class StepDuration:
    """One measured interval between two journal events."""

    hop: str
    count: int
    minimum_ms: int
    median_ms: int
    p95_ms: int
    maximum_ms: int

    def as_record(self) -> dict[str, Any]:
        return {
            "hop": self.hop,
            "count": self.count,
            "min_ms": self.minimum_ms,
            "median_ms": self.median_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.maximum_ms,
        }


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """Durations between pipeline steps, labelled with the clock that produced them."""

    #: Which clock. ``SimulatedClock`` means these are NOT latency measurements.
    clock_source: str
    steps: tuple[StepDuration, ...] = ()

    @property
    def is_observed(self) -> bool:
        """Whether these durations came from a real clock.

        False for every offline replay. A caller that needs observed latency must check this
        rather than reading the numbers, which is why it is a property and not a field.
        """
        return self.clock_source not in _SIMULATED_CLOCKS

    @property
    def label(self) -> str:
        return "latency" if self.is_observed else "step duration (simulated clock)"

    def summary(self) -> str:
        if not self.steps:
            return f"{self.label}: nothing measured (clock: {self.clock_source})"
        note = (
            ""
            if self.is_observed
            else " -- NOT a latency measurement; these are fixture step sizes and say nothing "
            "about behaviour against a real venue"
        )
        parts = ", ".join(f"{step.hop} p95={step.p95_ms}ms" for step in self.steps)
        return f"{self.label} (clock: {self.clock_source}): {parts}{note}"

    def as_record(self) -> dict[str, Any]:
        return {
            "clock_source": self.clock_source,
            "is_observed_latency": self.is_observed,
            "label": self.label,
            "steps": [step.as_record() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class InstrumentAttribution:
    """One instrument's contribution."""

    instrument: str
    quantity: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    mark_price: Decimal
    average_cost: Decimal

    @property
    def total_pnl(self) -> Decimal:
        return q_cash(self.realized_pnl + self.unrealized_pnl)

    def as_record(self) -> dict[str, str]:
        return {
            "instrument": self.instrument,
            "quantity": str(self.quantity),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "total_pnl": str(self.total_pnl),
            "mark_price": str(self.mark_price),
            "average_cost": str(self.average_cost),
        }


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """Where the session's P&L came from, exactly."""

    opening_equity: Decimal
    closing_equity: Decimal
    closing_cash: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    commission_paid: Decimal
    instruments: tuple[InstrumentAttribution, ...] = ()
    #: Fees are reported separately because they are a cost, not a trading result, and
    #: folding them into P&L hides how much of the result was eaten by friction.
    mark_count: int = 0

    @property
    def gross_pnl(self) -> Decimal:
        return q_cash(self.realized_pnl + self.unrealized_pnl)

    @property
    def net_pnl(self) -> Decimal:
        return q_cash(self.gross_pnl - self.commission_paid)

    @property
    def equity_change(self) -> Decimal:
        return q_cash(self.closing_equity - self.opening_equity)

    @property
    def instrument_total(self) -> Decimal:
        """The same total, summed from the per-instrument breakdown."""
        return q_cash(sum((item.total_pnl for item in self.instruments), ZERO))

    @property
    def breakdown_agrees(self) -> bool:
        """Whether the instrument breakdown matches the aggregate figures.

        Separate from reconciliation: a report can reconcile with the ledger while its own
        breakdown contradicts it, and that would be a different, quieter bug.
        """
        if not self.instruments:
            # Nothing to disagree with. An aggregate-only report is legitimate.
            return True
        return abs(self.instrument_total - self.gross_pnl) <= Decimal("0.01")

    @property
    def sum_of_parts(self) -> Decimal:
        """Everything attributed, net of commission.

        Built from the AGGREGATE realised and unrealised figures, not from the instrument
        breakdown. The first version summed the breakdown, which returned `-commission` for
        a report that carried aggregates but no per-instrument rows -- a wrong answer produced
        confidently. The breakdown is checked against the aggregate by
        :attr:\breakdown_agrees instead of being used as the source of truth.
        """
        return q_cash(self.realized_pnl + self.unrealized_pnl - self.commission_paid)

    @property
    def residual(self) -> Decimal:
        """Equity change minus the sum of the attributed parts.

        Reported rather than hidden. A non-zero residual means the attribution accounts for
        less than the ledger changed, and the usual cause is a real property of the event
        stream rather than an arithmetic error: `PositionChanged` is emitted when a position
        changes, carrying the mark that applied *then*, while `LedgerMarked` continues to be
        emitted as the mark moves. So the last position snapshot can be from an earlier instant
        than the final valuation, and the difference between those two marks is unattributed.

        Publishing a report that does not add up without saying so would be worse than not
        publishing one, so the residual has a name and appears in every rendering.
        """
        return q_cash(self.equity_change - self.sum_of_parts)

    def reconciles(self, *, tolerance: Decimal = Decimal("0.01")) -> bool:
        """Whether the components add up to the equity change.

        Off by more than a cent means the attribution accounts for less than the ledger moved.
        Tolerance is a cent because the ledger quantises cash to two places.
        """
        return abs(self.residual) <= tolerance

    def summary(self) -> str:
        if self.reconciles():
            verdict = "reconciles"
        else:
            verdict = (
                f"RESIDUAL {self.residual:+} unattributed - usually a PositionChanged snapshot "
                "taken at a different instant than the final LedgerMarked, not an arithmetic "
                "error"
            )
        return (
            f"P&L: equity {self.opening_equity} -> {self.closing_equity} "
            f"({self.equity_change:+}), gross {self.gross_pnl}, commission "
            f"{self.commission_paid}, net {self.net_pnl} [{verdict}]"
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "opening_equity": str(self.opening_equity),
            "closing_equity": str(self.closing_equity),
            "equity_change": str(self.equity_change),
            "closing_cash": str(self.closing_cash),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "gross_pnl": str(self.gross_pnl),
            "commission_paid": str(self.commission_paid),
            "net_pnl": str(self.net_pnl),
            "sum_of_parts": str(self.sum_of_parts),
            "instrument_total": str(self.instrument_total),
            "breakdown_agrees": self.breakdown_agrees,
            "residual": str(self.residual),
            "reconciles": self.reconciles(),
            "mark_count": self.mark_count,
            "instruments": [item.as_record() for item in self.instruments],
        }


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    """Everything measurable about one journal."""

    mode: str
    attribution: AttributionReport
    latency: LatencyReport
    decisions: int
    orders: int
    fills: int
    abstentions: int
    risk_refusals: int
    duration_seconds: int
    mark_count: int
    events: int
    facts: dict[str, str] = field(default_factory=dict)

    def digest(self) -> str:
        return digest_of(
            {
                "mode": self.mode,
                "decisions": self.decisions,
                "fills": self.fills,
                "closing_equity": str(self.attribution.closing_equity),
            }
        )

    def summary(self) -> str:
        return (
            f"session [{self.mode}]: {self.events} event(s) over {self.duration_seconds}s, "
            f"{self.decisions} decision(s) ({self.abstentions} abstention(s)), "
            f"{self.orders} order(s), {self.fills} fill(s), {self.risk_refusals} refusal(s); "
            f"{self.attribution.summary()}"
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "events": self.events,
            "duration_seconds": self.duration_seconds,
            "decisions": self.decisions,
            "abstentions": self.abstentions,
            "orders": self.orders,
            "fills": self.fills,
            "risk_refusals": self.risk_refusals,
            "mark_count": self.mark_count,
            "attribution": self.attribution.as_record(),
            "latency": self.latency.as_record(),
            "facts": dict(self.facts),
            "digest": self.digest(),
        }


def _payload(event: EventBase) -> dict[str, Any]:
    raw = getattr(event, "payload", None)
    if callable(raw):
        raw = raw()
    return dict(raw or {})


def _dec(value: object) -> Decimal:
    return ZERO if value is None else Decimal(str(value))


def measure_session(
    events: Iterable[EventBase],
    *,
    mode: str,
    clock_source: str = "SimulatedClock",
) -> SessionMetrics:
    """Measure one journal.

    ``clock_source`` defaults to ``SimulatedClock`` deliberately: the offline replay is the
    only thing that can run today, so the conservative label is the correct default. A paper
    session must pass ``clock_source="SystemClock"`` explicitly, and only then do the durations
    describe the real world.
    """
    ordered = tuple(events)
    if not ordered:
        return _empty(mode, clock_source)

    accumulator = _Accumulator()
    for event in ordered:
        _accumulate(event, accumulator)

    opening_equity = accumulator.marks[0][2] if accumulator.marks else ZERO
    closing_cash = accumulator.marks[-1][1] if accumulator.marks else ZERO
    closing_equity = accumulator.marks[-1][2] if accumulator.marks else ZERO

    instruments = tuple(
        InstrumentAttribution(
            instrument=instrument,
            quantity=data["quantity"],
            realized_pnl=data["realized_pnl"],
            unrealized_pnl=data["unrealized_pnl"],
            mark_price=data["mark_price"],
            average_cost=data["average_cost"],
        )
        for instrument, data in sorted(accumulator.positions.items())
    )

    attribution = AttributionReport(
        opening_equity=opening_equity,
        closing_equity=closing_equity,
        closing_cash=closing_cash,
        realized_pnl=q_cash(sum((item.realized_pnl for item in instruments), ZERO)),
        unrealized_pnl=q_cash(sum((item.unrealized_pnl for item in instruments), ZERO)),
        commission_paid=q_cash(accumulator.commission),
        instruments=instruments,
        mark_count=len(accumulator.marks),
    )

    steps = tuple(_step(hop, samples) for hop, samples in accumulator.durations.items() if samples)

    return SessionMetrics(
        mode=mode,
        attribution=attribution,
        latency=LatencyReport(clock_source=clock_source, steps=steps),
        decisions=accumulator.decisions,
        orders=accumulator.orders,
        fills=accumulator.fills,
        abstentions=accumulator.abstentions,
        risk_refusals=accumulator.refusals,
        duration_seconds=int((ordered[-1].occurred_at - ordered[0].occurred_at).total_seconds()),
        mark_count=len(accumulator.marks),
        events=len(ordered),
        facts=_session_facts(mode, clock_source=clock_source),
    )


@dataclass
class _Accumulator:
    """Running totals for one pass over the journal."""

    durations: dict[str, list[int]] = field(default_factory=lambda: {hop[0]: [] for hop in _HOPS})
    open_hops: dict[str, dict[str, dt.datetime]] = field(
        default_factory=lambda: {hop[0]: {} for hop in _HOPS}
    )
    marks: list[tuple[dt.datetime, Decimal, Decimal]] = field(default_factory=list)
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    commission: Decimal = ZERO
    decisions: int = 0
    orders: int = 0
    fills: int = 0
    abstentions: int = 0
    refusals: int = 0


def _accumulate(event: EventBase, acc: _Accumulator) -> None:
    """Fold one event into the running totals."""
    kind = type(event).__name__
    payload = _payload(event)
    moment = event.occurred_at

    for hop, start_kind, end_kind in _HOPS:
        if kind == start_kind:
            key = str(
                payload.get("intent_id") or payload.get("decision_id") or len(acc.open_hops[hop])
            )
            acc.open_hops[hop][key] = moment
        elif kind == end_kind:
            key = str(payload.get("intent_id") or payload.get("decision_id") or "")
            started = acc.open_hops[hop].pop(key, None)
            if started is None and acc.open_hops[hop]:
                # The end event carries no id we can match, so pair it with the oldest
                # unmatched start. Better than dropping the measurement, and the reported
                # count makes any mismatch visible.
                _, started = acc.open_hops[hop].popitem()
            if started is not None:
                acc.durations[hop].append(int((moment - started).total_seconds() * 1000))

    if kind == "DecisionProposed":
        acc.decisions += 1
        if str(payload.get("action", "")).upper() == "NO_TRADE":
            acc.abstentions += 1
    elif kind == "OrderIntentCreated":
        acc.orders += 1
    elif kind in {"OrderFilled", "OrderPartiallyFilled"}:
        acc.fills += 1
        acc.commission += _dec(payload.get("commission"))
    elif kind == "RiskEvaluated":
        if str(payload.get("outcome", "")).upper() == "DENY":
            acc.refusals += 1
    elif kind == "LedgerMarked":
        acc.marks.append(
            (
                moment,
                q_cash(_dec(payload.get("cash"))),
                q_cash(_dec(payload.get("equity"))),
            )
        )
    elif kind == "PositionChanged":
        acc.positions[str(payload.get("instrument", ""))] = {
            "quantity": int(payload.get("quantity", 0) or 0),
            "realized_pnl": q_cash(_dec(payload.get("realized_pnl"))),
            "unrealized_pnl": q_cash(_dec(payload.get("unrealized_pnl"))),
            "mark_price": _dec(payload.get("mark_price")),
            "average_cost": _dec(payload.get("average_cost")),
        }


def _step(hop: str, samples: Sequence[int]) -> StepDuration:
    ordered = sorted(samples)
    index = min(int(len(ordered) * 0.95), len(ordered) - 1)
    return StepDuration(
        hop=hop,
        count=len(ordered),
        minimum_ms=ordered[0],
        median_ms=int(statistics.median(ordered)),
        p95_ms=ordered[index],
        maximum_ms=ordered[-1],
    )


def _session_facts(mode: str, *, clock_source: str) -> dict[str, str]:
    """Facts an operator must see alongside the numbers.

    Keyword-only for ``clock_source``: it is the difference between a measurement and a
    misleading one, so it should not be passable positionally by accident.
    """
    observed = clock_source not in _SIMULATED_CLOCKS
    return {
        "journal_mode": mode,
        "clock_source": clock_source,
        "durations_are_observed": str(observed),
        "note": (
            "These durations came from a real clock."
            if observed
            else "These durations came from a SIMULATED clock. They are fixture step sizes and "
            "are NOT a latency measurement. Observed latency needs a real paper session, which "
            "needs a write-scoped broker grant this project does not hold."
        ),
    }


def _empty(mode: str, clock_source: str) -> SessionMetrics:
    return SessionMetrics(
        mode=mode,
        attribution=AttributionReport(
            opening_equity=ZERO,
            closing_equity=ZERO,
            closing_cash=ZERO,
            realized_pnl=ZERO,
            unrealized_pnl=ZERO,
            commission_paid=ZERO,
        ),
        latency=LatencyReport(clock_source=clock_source),
        decisions=0,
        orders=0,
        fills=0,
        abstentions=0,
        risk_refusals=0,
        duration_seconds=0,
        mark_count=0,
        events=0,
        facts=_session_facts(mode, clock_source=clock_source),
    )


def attribution_by_decision(
    events: Iterable[EventBase], *, mode: str
) -> tuple[dict[str, Any], ...]:
    """Per-decision contribution, joined through the lineage.

    The lineage already links a decision to its fills; this adds the money. Attribution per
    *decision* is what answers "which idea made or lost the money", which per-instrument
    attribution cannot.
    """
    ordered = tuple(events)
    lineage = build_lineage(ordered, mode=mode)
    return tuple(
        {
            "decision_id": record.decision_id,
            "action": record.action,
            "instrument": record.instrument,
            "filled_quantity": record.filled_quantity,
            "traded_notional": str(record.traded_notional),
            "reached_the_venue": record.reached_the_venue,
            "was_refused": record.was_refused,
        }
        for record in lineage.records
    )
