"""Shared test fixtures.

Everything here is offline: no network, no broker SDK, no credentials.
"""

from __future__ import annotations

import datetime as dt
import shutil
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

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
from tigerquant.core.ids import IdFactory
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.journal import MemoryJournal
from tigerquant.evidence.snapshot import SnapshotBuilder
from tigerquant.evidence.sources import SourceRegistry, default_test_registry
from tigerquant.evidence.store import EvidenceStore
from tigerquant.execution.fake_broker import DeterministicFakeBroker
from tigerquant.execution.intent import OrderProjection
from tigerquant.execution.service import ExecutionService, StartupGate
from tigerquant.market.fixtures import ScenarioFixture, load_scenario
from tigerquant.market.session import TradingSession
from tigerquant.portfolio.ledger import PortfolioLedger
from tigerquant.risk.approvals import ApprovalRegistry, OrderIntentView
from tigerquant.risk.gateway import RiskGateway
from tigerquant.risk.metrics import RiskMetrics
from tigerquant.risk.policy import RiskPolicy
from tigerquant.risk.state import RiskState

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "scenarios"

#: A Monday inside the regular session and the approved order window (09:40 New York).
SESSION_START = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """A per-test scratch directory inside the repository.

    Overrides pytest's built-in fixture, which places directories under the system temp
    area. Under the DSH workspace sandbox that path is not writable, and a test suite that
    cannot create its own scratch space would be a false failure. The directory is removed
    afterwards, and the parent is git-ignored.
    """
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in request.node.name)
    base = Path(__file__).resolve().parent / "_tmp" / safe[:80]
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def scenario_path() -> Path:
    return FIXTURES_DIR / "trend_up.yaml"


@pytest.fixture
def scenario(scenario_path: Path) -> ScenarioFixture:
    return load_scenario(scenario_path)


@pytest.fixture
def session() -> TradingSession:
    return TradingSession()


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(SESSION_START)


@pytest.fixture
def ids(clock: SimulatedClock) -> IdFactory:
    return IdFactory("bt", clock)


@pytest.fixture
def sources() -> SourceRegistry:
    """A registry that knows the synthetic sources the tests use."""
    return default_test_registry()


@pytest.fixture
def store(sources: SourceRegistry) -> EvidenceStore:
    return EvidenceStore(sources)


@pytest.fixture
def builder(store: EvidenceStore) -> SnapshotBuilder:
    return SnapshotBuilder(store, max_age_seconds=900)


@pytest.fixture
def ledger() -> PortfolioLedger:
    return PortfolioLedger(initial_cash=Decimal("100000.00"))


@pytest.fixture
def policy() -> RiskPolicy:
    return RiskPolicy(
        policy_version="test-policy",
        universe_version="test-universe",
        allowed_instruments=("SPY", "QQQ"),
        gross_exposure_fraction=Decimal("0.50"),
        max_symbol_fraction=Decimal("0.10"),
        max_order_fraction=Decimal("0.10"),
        max_mark_age_seconds=900,
        max_orders_per_cycle=1,
    )


@pytest.fixture
def risk_state() -> RiskState:
    return RiskState()


@pytest.fixture
def registry() -> ApprovalRegistry:
    return ApprovalRegistry()


@pytest.fixture
def gateway(policy: RiskPolicy, risk_state: RiskState, registry: ApprovalRegistry) -> RiskGateway:
    return RiskGateway(policy=policy, state=risk_state, registry=registry)


@pytest.fixture
def memory_journal() -> MemoryJournal:
    return MemoryJournal(Mode.BACKTEST)


@pytest.fixture
def emitter(memory_journal: MemoryJournal, ids: IdFactory, clock: SimulatedClock) -> EventEmitter:
    return EventEmitter(
        mode=Mode.BACKTEST, sink=memory_journal, ids=ids, clock=clock, correlation_id="test"
    )


@pytest.fixture
def projection() -> OrderProjection:
    return OrderProjection()


@pytest.fixture
def order_view() -> OrderIntentView:
    return OrderIntentView(
        intent_id="bt-oi_00000000000000",
        decision_id="bt-dec_00000000000000",
        instrument="SPY",
        side=OrderSide.BUY,
        quantity=8,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        client_order_id="TQ-bt-abcdef123456",
    )


@pytest.fixture
def startup_gate() -> StartupGate:
    gate = StartupGate()
    gate.open(reason="test")
    return gate


@pytest.fixture
def caps() -> object:
    return capabilities_for(Mode.BACKTEST)


@pytest.fixture
def broker(scenario: ScenarioFixture) -> DeterministicFakeBroker:
    marks: dict[str, Decimal] = {"SPY": Decimal("600.00"), "QQQ": Decimal("500.00")}
    return DeterministicFakeBroker(scenario.broker, mark_provider=marks.get)


@pytest.fixture
def execution_service(
    broker: DeterministicFakeBroker,
    gateway: RiskGateway,
    emitter: EventEmitter,
    ledger: PortfolioLedger,
    projection: OrderProjection,
    startup_gate: StartupGate,
    ids: IdFactory,
) -> ExecutionService:
    return ExecutionService(
        broker=broker,
        gateway=gateway,
        emitter=emitter,
        ledger=ledger,
        projection=projection,
        gate=startup_gate,
        ids=ids,
        mode_caps=capabilities_for(Mode.BACKTEST),
        submission_target=SubmissionTarget.SIMULATION,
    )


def allow_risk(
    gateway: RiskGateway,
    order: OrderIntentView,
    *,
    portfolio: PortfolioLedger,
    approved_at: dt.datetime,
    marks: dict[str, Decimal],
    available: dict[str, dt.datetime],
) -> tuple[str, object]:
    """Helper: evaluate and issue an approval for a known-good order."""
    from tigerquant.risk.gateway import RiskRequest

    request = RiskRequest(
        order=order,
        marks=marks,
        mark_available_at=available,
        as_of=approved_at,
        startup_gate_open=True,
        orders_this_cycle=0,
        snapshot_usable=True,
        session=TradingSession(),
        submission_target=SubmissionTarget.SIMULATION,
        mode=Mode.BACKTEST,
        metrics=RiskMetrics.baseline(equity=portfolio.cash),
    )
    outcome = gateway.evaluate(request, portfolio=portfolio)
    assert outcome.allowed, outcome.detail
    approval = gateway.issue(
        outcome,
        order=order,
        portfolio_digest=outcome.portfolio_digest,
        approved_at=approved_at,
        risk_event_id="ev-test",
    )
    return approval.token, outcome


__all__ = [
    "FIXTURES_DIR",
    "SESSION_START",
    "ReasonCode",
    "allow_risk",
]


def pytest_collection_finish(session: object) -> None:
    """Record what collection actually saw, for `tigerquant gates` to check the documents against.

    A CHILD pytest process does not complete in this environment, so the counts cannot be obtained
    by asking a second interpreter. Taking them from the run that actually happened is also the
    better fact: the documented numbers are compared against real collection, not a hypothetical
    one.

    Markers are read per item. A module-level `pytestmark` IS reported by `iter_markers()`, which a
    probe confirmed for the twenty `requires_db` tests before this was written.
    """
    import datetime as dt
    import json
    from pathlib import Path

    # ONLY a full-suite run may write the record. Running a single file would otherwise clobber
    # it, and the documented counts describe the WHOLE suite -- so the check would start comparing
    # full-suite claims against a subset and report a stale count that was never stale. Found by
    # running one test file after the count check was working.
    args = [str(arg) for arg in getattr(getattr(session, "config", None), "args", []) or []]
    full_suite = not args or all(arg in {"tests", "tests/"} for arg in args)
    if not full_suite:
        return

    items = list(getattr(session, "items", []) or [])
    by_marker: dict[str, int] = {}
    for item in items:
        iterator = getattr(item, "iter_markers", None)
        found = [mark.name for mark in iterator()] if callable(iterator) else []
        for name in set(found):
            by_marker[name] = by_marker.get(name, 0) + 1

    total = len(items)
    requires_db = by_marker.get("requires_db", 0)
    payload = {
        "total": total,
        "requires_db": requires_db,
        "not_requires_db": total - requires_db,
        "written_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "by_marker": dict(sorted(by_marker.items())),
    }

    out = Path("out")
    out.mkdir(parents=True, exist_ok=True)
    (out / "collected_counts.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
