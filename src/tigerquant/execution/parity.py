"""Execution parity: do two broker implementations behave the same on the same inputs?

CR-04's gate is "same deterministic strategy events in backtest and paper; adapter parity
tests". This module is the mechanism. It drives two :class:`~tigerquant.execution.broker.BrokerProtocol`
implementations through an identical ordered sequence of operations and compares what came
back.

The comparison is **layered**, and that is the point. Two execution venues will never agree on
everything — a simulator fills instantly at the mark while a real venue fills later at a
different price, or only partially. A harness that demanded byte equality would be useless,
and one that smoothed differences away would be worse. So it reports three kinds of
divergence:

* **Structural** — a different number of operations, different states, one side rejecting what
  the other accepted. These are *failures*: the two paths are not implementing the same
  contract, and a strategy's decisions would mean different things in each.
* **Labelled** — the same state reached with different numbers: a fill price, a commission, a
  partial-fill fraction. These are *expected* between a simulator and a venue, and they are
  reported rather than hidden, each with the field and both values.
* **Timing** — the same state at a different instant. Reported separately because a simulator
  that fills "instantly" and a venue that fills in 40ms are both correct.

A verdict of ``comparable`` means the structural contract matched. It never means the two
venues are interchangeable, and the report says so.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import digest_of
from tigerquant.execution.broker import (
    BrokerFill,
    BrokerOrderState,
    BrokerOrderView,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tigerquant.execution.broker import BrokerProtocol
    from tigerquant.risk.approvals import ApprovedOrder


class OrderCommandKind(StrEnum):
    """The operations a parity scenario can contain."""

    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    QUERY = "QUERY"


@dataclass(frozen=True, slots=True)
class OrderCommand:
    """One operation in a parity scenario.

    Defined here rather than in the broker contract because it is a *test* concept: a broker
    never receives one of these, and putting it in the protocol would imply it did.
    """

    kind: OrderCommandKind
    client_order_id: str
    at: dt.datetime


__all__ = [
    "DIVERGENCE_KINDS",
    "Divergence",
    "ExecutionTrace",
    "OrderCommand",
    "OrderCommandKind",
    "ParityReport",
    "TraceStep",
    "compare_traces",
    "record_trace",
]

#: Divergence kinds, in increasing order of concern.
DIVERGENCE_KINDS = ("timing", "labelled", "structural")

#: Fields whose difference is a *numeric* difference between venues rather than a contract
#: violation. A venue may legitimately fill at a worse price.
_LABELLED_FIELDS = frozenset(
    {"price", "average_fill_price", "commission", "filled_quantity", "remaining_quantity"}
)


@dataclass(frozen=True, slots=True)
class TraceStep:
    """One operation and everything the broker reported back."""

    index: int
    command: str
    client_order_id: str
    #: The state the broker reported, or ``None`` when the broker returned nothing.
    state: str | None
    broker_order_id: str | None
    filled_quantity: int
    remaining_quantity: int
    average_fill_price: Decimal | None
    fills: tuple[str, ...]
    message: str = ""

    def structural_key(self) -> tuple[object, ...]:
        """The part that must match between two venues for the contract to hold.

        Numbers that legitimately differ between a simulator and a real venue are excluded;
        everything else — how many fills, what state, whether an order id was issued — is in.
        """
        return (
            self.command,
            self.client_order_id,
            self.state,
            len(self.fills),
            self.broker_order_id is not None,
        )


@dataclass(frozen=True, slots=True)
class ExecutionTrace:
    """The full record of one broker driven through one scenario."""

    broker: str
    steps: tuple[TraceStep, ...] = field(default_factory=tuple)

    def digest(self) -> str:
        return digest_of(
            {
                "broker": self.broker,
                "steps": [step.structural_key() for step in self.steps],
            }
        )

    def states(self) -> tuple[str, ...]:
        return tuple(step.state or "(none)" for step in self.steps)

    def describe(self) -> str:
        return f"{self.broker}: {len(self.steps)} step(s) -> {' -> '.join(self.states())}"


@dataclass(frozen=True, slots=True)
class Divergence:
    """One difference between two traces."""

    kind: str
    index: int
    field: str
    left: object
    right: object

    def describe(self) -> str:
        return f"[{self.kind}] step {self.index} {self.field}: {self.left!r} vs {self.right!r}"


@dataclass(frozen=True, slots=True)
class ParityReport:
    """The outcome of comparing two traces."""

    left: ExecutionTrace
    right: ExecutionTrace
    divergences: tuple[Divergence, ...] = ()

    @property
    def structural(self) -> tuple[Divergence, ...]:
        return tuple(d for d in self.divergences if d.kind == "structural")

    @property
    def labelled(self) -> tuple[Divergence, ...]:
        """Numeric differences between venues. Expected, reported, never smoothed."""
        return tuple(d for d in self.divergences if d.kind == "labelled")

    @property
    def timing(self) -> tuple[Divergence, ...]:
        return tuple(d for d in self.divergences if d.kind == "timing")

    @property
    def comparable(self) -> bool:
        """Whether the two paths implement the same contract.

        Deliberately *not* named "equal": a comparable pair still has labelled differences,
        and calling that equality would invite someone to treat two venues as interchangeable.
        """
        return not self.structural

    def summary(self) -> str:
        if self.comparable and not self.divergences:
            return f"identical: {self.left.broker} == {self.right.broker} over {len(self.left.steps)} step(s)"
        if self.comparable:
            return (
                f"comparable: {self.left.broker} vs {self.right.broker} over "
                f"{len(self.left.steps)} step(s), {len(self.labelled)} labelled difference(s) "
                f"(prices, quantities, commissions), {len(self.timing)} timing difference(s). "
                "Comparable does NOT mean interchangeable."
            )
        return (
            f"STRUCTURAL MISMATCH: {len(self.structural)} difference(s) between "
            f"{self.left.broker} and {self.right.broker}. These are contract violations, not "
            "venue characteristics."
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "left": self.left.broker,
            "right": self.right.broker,
            "comparable": self.comparable,
            "structural": [d.describe() for d in self.structural],
            "labelled": [d.describe() for d in self.labelled],
            "timing": [d.describe() for d in self.timing],
            "left_digest": self.left.digest(),
            "right_digest": self.right.digest(),
        }


# ---------------------------------------------------------------------------
# Driving a broker
# ---------------------------------------------------------------------------
def record_trace(
    broker: BrokerProtocol,
    commands: Sequence[OrderCommand],
    *,
    approvals: dict[str, ApprovedOrder] | None = None,
    label: str | None = None,
) -> ExecutionTrace:
    """Drive ``broker`` through ``commands`` and record what it reported.

    The command list is the scenario. Both brokers must be given the *same* list in the same
    order, which is what makes the comparison meaningful: any difference in the result comes
    from the broker, not from the harness.
    """
    steps: list[TraceStep] = []
    approvals = approvals or {}

    for index, command in enumerate(commands):
        steps.append(_execute(broker, command, index=index, approvals=approvals))

    return ExecutionTrace(broker=label or type(broker).__name__, steps=tuple(steps))


def _execute(
    broker: BrokerProtocol,
    command: OrderCommand,
    *,
    index: int,
    approvals: dict[str, ApprovedOrder],
) -> TraceStep:
    if command.kind is OrderCommandKind.SUBMIT:
        approval = approvals.get(command.client_order_id)
        if approval is None:
            return TraceStep(
                index=index,
                command=command.kind.value,
                client_order_id=command.client_order_id,
                state=None,
                broker_order_id=None,
                filled_quantity=0,
                remaining_quantity=0,
                average_fill_price=None,
                fills=(),
                message="no approval supplied for this command; skipped in both traces",
            )
        ack = broker.submit(approval, at=command.at)
        return TraceStep(
            index=index,
            command=command.kind.value,
            client_order_id=ack.client_order_id,
            state=ack.state.value,
            broker_order_id=ack.broker_order_id,
            filled_quantity=sum(fill.quantity for fill in ack.fills),
            remaining_quantity=ack.remaining,
            average_fill_price=_average(ack.fills),
            fills=tuple(fill.exec_id for fill in ack.fills),
            message=ack.message,
        )

    if command.kind is OrderCommandKind.CANCEL:
        cancelled = broker.cancel(client_order_id=command.client_order_id, at=command.at)
        return TraceStep(
            index=index,
            command=command.kind.value,
            client_order_id=cancelled.client_order_id,
            state=cancelled.state.value,
            broker_order_id=None,
            filled_quantity=0,
            remaining_quantity=cancelled.canceled_quantity,
            average_fill_price=None,
            fills=(),
            message=cancelled.message,
        )

    # QUERY
    view: BrokerOrderView | None = broker.query(client_order_id=command.client_order_id)
    if view is None:
        return TraceStep(
            index=index,
            command=command.kind.value,
            client_order_id=command.client_order_id,
            state=None,
            broker_order_id=None,
            filled_quantity=0,
            remaining_quantity=0,
            average_fill_price=None,
            fills=(),
            message="not found",
        )
    return TraceStep(
        index=index,
        command=command.kind.value,
        client_order_id=view.client_order_id,
        state=view.state.value,
        broker_order_id=view.broker_order_id,
        filled_quantity=view.filled_quantity,
        remaining_quantity=view.remaining,
        # The price must be carried here too. Leaving it None meant a query could not expose a
        # price difference, so two venues filling the same quantity at different prices were
        # reported as "identical" -- the exact smoothing this module promises not to do. The
        # test comparing two scripted prices is what caught it.
        average_fill_price=_average(view.fills),
        fills=tuple(fill.exec_id for fill in view.fills),
    )


def _average(fills: Sequence[BrokerFill]) -> Decimal | None:
    if not fills:
        return None
    total_quantity = sum(fill.quantity for fill in fills)
    if total_quantity == 0:
        return None
    notional = sum((fill.price * fill.quantity for fill in fills), Decimal(0))
    return (notional / total_quantity).quantize(Decimal("0.0001"))


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------
def compare_traces(left: ExecutionTrace, right: ExecutionTrace) -> ParityReport:
    """Compare two traces, classifying every divergence by severity."""
    divergences: list[Divergence] = []

    if len(left.steps) != len(right.steps):
        divergences.append(
            Divergence(
                kind="structural",
                index=-1,
                field="step_count",
                left=len(left.steps),
                right=len(right.steps),
            )
        )

    for left_step, right_step in zip(left.steps, right.steps, strict=False):
        if left_step.structural_key() != right_step.structural_key():
            for name in ("state", "broker_order_id", "fills"):
                lvalue = getattr(left_step, name)
                rvalue = getattr(right_step, name)
                if name == "broker_order_id":
                    lvalue, rvalue = lvalue is not None, rvalue is not None
                if lvalue != rvalue:
                    divergences.append(
                        Divergence(
                            kind="structural",
                            index=left_step.index,
                            field=name,
                            left=lvalue,
                            right=rvalue,
                        )
                    )

        # Numeric differences: expected between venues, always reported.
        for name in _LABELLED_FIELDS:
            lvalue = getattr(left_step, name, None)
            rvalue = getattr(right_step, name, None)
            if lvalue is None or rvalue is None:
                continue
            if lvalue != rvalue:
                divergences.append(
                    Divergence(
                        kind="labelled",
                        index=left_step.index,
                        field=name,
                        left=lvalue,
                        right=rvalue,
                    )
                )

        # A message difference alone is timing/informational.
        if left_step.message != right_step.message:
            divergences.append(
                Divergence(
                    kind="timing",
                    index=left_step.index,
                    field="message",
                    left=left_step.message,
                    right=right_step.message,
                )
            )

    return ParityReport(left=left, right=right, divergences=tuple(divergences))


#: Broker states that PROVE no exposure resulted.
#:
#: **`UNKNOWN` IS NOT ONE OF THEM, AND ITS ABSENCE IS THE POINT.** A timeout or transport error after a
#: submission may have been ACCEPTED by the venue (ADR-0008), so "we do not know" is not "nothing
#: happened". Treating it as no exposure is the failure AGENTS.md invariant 8 exists to prevent: never
#: repeat a submission on an uncertain outcome, and reconcile before a restart permits new exposure.
#:
#: The previous version of this helper included `UNKNOWN` and its docstring said "means no exposure
#: resulted". It was harmless only because nothing called it.
_NO_EXPOSURE_STATES: frozenset[BrokerOrderState] = frozenset(
    {BrokerOrderState.REJECTED, BrokerOrderState.CANCELED}
)


def empty_or_unfilled(state: BrokerOrderState) -> bool:
    """True when a broker state PROVES no exposure resulted.

    **`UNKNOWN` returns False**, because an outcome nobody established is not evidence of nothing.
    A caller that wants "no exposure OR unresolved" must say so itself rather than getting it by
    accident -- the two lead to different actions: one is safe to forget, the other must be reconciled.
    """
    return state in _NO_EXPOSURE_STATES


def at(instant: dt.datetime) -> dt.datetime:
    """Normalise an instant to UTC at the adapter boundary (ADR-0010 §5)."""
    return instant if instant.tzinfo else instant.replace(tzinfo=dt.UTC)
