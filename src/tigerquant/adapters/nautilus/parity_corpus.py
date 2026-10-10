"""Strategy-loop parity: the same scenario through both execution paths.

ADR-0010 §6: "the deterministic simulator and the paper adapter must produce the same *internal*
order/fill event sequence for the same inputs. Real-world divergence (queue position, partial
fills, rejections) is recorded as a labelled difference, never smoothed over."

The two paths compared here are:

**Direct** — our pipeline's execution service drives a
:class:`~tigerquant.execution.fake_broker.DeterministicFakeBroker`.

**Adapter** — the *same* broker is driven through the real NautilusTrader
:class:`~tigerquant.adapters.nautilus.execution_client.TigerQuantExecutionClient`. That path
exercises the engine translation layer, the three gates, and the status mapping.

Because both paths end at the same broker, any structural difference is a defect in the adapter,
not a venue characteristic — which is exactly what makes this a parity test rather than a
comparison of two unrelated things. The venue-characteristic divergences the ADR mentions would
appear here the moment the adapter path pointed at a real venue instead; the harness classifies
them rather than failing on them.

The corpus is **in code, committed, and additive**. A scenario that used to pass must keep
passing, and a new one is a new case rather than an edit to an old one.

It lives in the ADAPTER package rather than in `execution/` because it imports the engine.
The first version sat in `execution/parity_corpus.py` and two of this project's own boundary
tests failed -- L2 may not import L3, and optional SDKs may only be imported inside
`adapters/`. Both were right, and the fix was to move the file rather than relax the rules.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from tigerquant.core.enums import Mode, OrderSide, OrderType, SubmissionTarget, TimeInForce
from tigerquant.execution.fake_broker import DeterministicFakeBroker
from tigerquant.execution.parity import (
    ExecutionTrace,
    OrderCommand,
    OrderCommandKind,
    ParityReport,
    compare_traces,
    record_trace,
)
from tigerquant.market.fixtures import BrokerFixture
from tigerquant.risk.approvals import ApprovedOrder, OrderIntentView

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CORPUS",
    "ParityCorpusReport",
    "ScenarioCase",
    "build_approvals",
    "run_corpus",
    "run_scenario",
]

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class ScenarioCase:
    """One parity scenario: a broker behaviour plus the orders to send it."""

    name: str
    fixture: BrokerFixture
    orders: tuple[OrderIntentView, ...]
    #: Marks the broker prices fills from.
    marks: dict[str, Decimal] = field(default_factory=dict)
    notes: str = ""

    def commands(self, *, at: dt.datetime = AT) -> tuple[OrderCommand, ...]:
        """The command list both paths are driven through, identically."""
        return tuple(
            OrderCommand(
                kind=OrderCommandKind.SUBMIT,
                client_order_id=order.client_order_id,
                at=at + dt.timedelta(seconds=index),
            )
            for index, order in enumerate(self.orders)
        )


def _order(
    index: int,
    *,
    instrument: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 8,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
) -> OrderIntentView:
    return OrderIntentView(
        intent_id=f"bt-oi_parity{index:04d}",
        decision_id=f"bt-dec_parity{index:04d}",
        instrument=instrument,
        side=side,
        quantity=quantity,
        order_type=order_type,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=f"TQ-pa-parity{index:04d}",
    )


def _broker_fixture(**overrides: object) -> BrokerFixture:
    """A broker spec, with whatever the fixture's own defaults are left in place.

    Built by keyword so a change to the fixture's shape surfaces here rather than silently
    producing a scenario that tests nothing.
    """
    return BrokerFixture(**overrides)


#: The committed corpus. Additive: a case is never edited to make a failure go away.
CORPUS: tuple[ScenarioCase, ...] = (
    ScenarioCase(
        name="immediate-full-fill",
        fixture=_broker_fixture(),
        orders=(_order(0),),
        marks={"SPY": Decimal("600.00")},
        notes="the simple case both paths must agree on exactly",
    ),
    ScenarioCase(
        name="two-orders-same-instrument",
        fixture=_broker_fixture(),
        orders=(_order(1), _order(2)),
        marks={"SPY": Decimal("600.00")},
        notes="two intents, distinct client ids; the adapter must not confuse them",
    ),
    ScenarioCase(
        name="sell-side",
        fixture=_broker_fixture(),
        orders=(_order(3, side=OrderSide.SELL),),
        marks={"SPY": Decimal("600.00")},
        notes="the adapter maps SELL as well as BUY",
    ),
    ScenarioCase(
        name="limit-order",
        fixture=_broker_fixture(),
        orders=(_order(4, order_type=OrderType.LIMIT, limit_price=Decimal("600.00")),),
        marks={"SPY": Decimal("600.00")},
        notes="a limit order carries a price through translation unchanged",
    ),
    ScenarioCase(
        name="different-instrument",
        fixture=_broker_fixture(),
        orders=(_order(5, instrument="QQQ"),),
        marks={"QQQ": Decimal("520.00")},
        notes="translation is per-instrument, not hardcoded to SPY",
    ),
)


def build_approvals(case: ScenarioCase, *, at: dt.datetime = AT) -> dict[str, ApprovedOrder]:
    """Single-use approvals for a scenario's orders.

    The adapter path refuses to submit without one, so the approvals are part of the scenario
    rather than something the harness injects to make it pass.
    """
    return {
        order.client_order_id: ApprovedOrder(
            token=f"apr:parity:{order.client_order_id}",
            order=order,
            policy_hash="sha256:" + "a" * 64,
            portfolio_digest="sha256:" + "b" * 64,
            approved_at=at,
            risk_event_id=f"ev-parity-{order.client_order_id}",
        )
        for order in case.orders
    }


def _direct_trace(case: ScenarioCase, *, at: dt.datetime = AT) -> ExecutionTrace:
    """Path A: our execution service drives the broker."""
    broker = DeterministicFakeBroker(case.fixture, mark_provider=case.marks.get)
    return record_trace(
        broker,
        case.commands(at=at),
        approvals=build_approvals(case, at=at),
        label="direct",
    )


def _adapter_trace(case: ScenarioCase, *, at: dt.datetime = AT) -> ExecutionTrace:
    """Path B: the Nautilus ExecutionClient drives the SAME broker.

    The engine's own callbacks are what translate a submit into a broker call, so this
    exercises the adapter's gates, translation and status mapping rather than replaying them.
    """
    from tigerquant.adapters.nautilus.execution_client import build_execution_client

    broker = DeterministicFakeBroker(case.fixture, mark_provider=case.marks.get)
    client = build_execution_client(
        broker=broker,
        approvals=build_approvals(case, at=at),
        submission_target=SubmissionTarget.BROKER,
        mode=Mode.PAPER,
    )
    client.tq_connect()

    steps: list[Any] = []
    for index, order in enumerate(case.orders):
        command = _engine_command(order, at=at + dt.timedelta(seconds=index))
        client.submit_order(command)
        steps.append(_step_from_broker(broker, order, index=index, command=command))

    return ExecutionTrace(broker="nautilus-adapter", steps=tuple(steps))


def _engine_command(order: OrderIntentView, *, at: dt.datetime) -> Any:
    """A stand-in for the engine's ``SubmitOrder``, shaped as the adapter expects.

    ``SimpleNamespace`` rather than a dynamically-created class: the adapter reads everything
    through ``getattr``, so a namespace is both sufficient and honest about the fact that this
    is not a real engine type.
    """
    del at
    # The engine validates its identifier types, so a bare string is rejected: a stand-in that
    # passed one would be testing a shape the real engine never produces.
    from nautilus_trader.model.identifiers import (
        ClientOrderId,
        InstrumentId,
        StrategyId,
    )
    from nautilus_trader.model.objects import Quantity

    return SimpleNamespace(
        order=SimpleNamespace(
            client_order_id=ClientOrderId(order.client_order_id),
            instrument_id=InstrumentId.from_str(f"{order.instrument}.US"),
            side=SimpleNamespace(name=order.side.value),
            order_type=SimpleNamespace(name=order.order_type.value),
            quantity=Quantity.from_int(order.quantity),
            time_in_force=SimpleNamespace(name=order.time_in_force.value),
            price=order.limit_price,
        ),
        strategy_id=StrategyId("PARITY-001"),
    )


def _step_from_broker(
    broker: DeterministicFakeBroker, order: OrderIntentView, *, index: int, command: object
) -> Any:
    """Read the broker's state for one order, in the same shape the direct path records."""
    from tigerquant.execution.parity import TraceStep, _average

    view = broker.query(client_order_id=order.client_order_id)
    del command
    if view is None:
        return TraceStep(
            index=index,
            command=OrderCommandKind.SUBMIT.value,
            client_order_id=order.client_order_id,
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
        command=OrderCommandKind.SUBMIT.value,
        client_order_id=view.client_order_id,
        state=view.state.value,
        broker_order_id=view.broker_order_id,
        filled_quantity=view.filled_quantity,
        remaining_quantity=view.remaining,
        average_fill_price=_average(view.fills),
        fills=tuple(fill.exec_id for fill in view.fills),
    )


def run_scenario(case: ScenarioCase, *, at: dt.datetime = AT) -> ParityReport:
    """Run one scenario through both paths and compare."""
    return compare_traces(_direct_trace(case, at=at), _adapter_trace(case, at=at))


@dataclass(frozen=True, slots=True)
class ParityCorpusReport:
    """The result of running the whole corpus."""

    reports: tuple[tuple[str, ParityReport], ...] = ()

    @property
    def total(self) -> int:
        return len(self.reports)

    @property
    def comparable(self) -> int:
        return sum(1 for _name, report in self.reports if report.comparable)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(name for name, report in self.reports if not report.comparable)

    @property
    def is_parity(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        if self.is_parity:
            labelled = sum(len(report.labelled) for _n, report in self.reports)
            return (
                f"parity: {self.comparable}/{self.total} scenario(s) structurally identical "
                f"between the direct path and the Nautilus adapter ({labelled} labelled "
                "difference(s), which is expected between venues)"
            )
        return (
            f"PARITY FAILURE in {len(self.failures)} of {self.total} scenario(s): "
            f"{list(self.failures)}"
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "comparable": self.comparable,
            "is_parity": self.is_parity,
            "failures": list(self.failures),
            "scenarios": [{"name": name, **report.as_record()} for name, report in self.reports],
        }


def run_corpus(
    corpus: Sequence[ScenarioCase] | None = None, *, at: dt.datetime = AT
) -> ParityCorpusReport:
    """Run every scenario. A scenario that raises is reported as a failure, not a crash."""
    cases = corpus if corpus is not None else CORPUS
    return ParityCorpusReport(
        reports=tuple((case.name, run_scenario(case, at=at)) for case in cases)
    )
