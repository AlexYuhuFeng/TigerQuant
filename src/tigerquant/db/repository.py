"""Projection repository: one contract, two implementations.

The journal (ADR-0004) is authoritative; everything here is a **rebuildable projection**
of it (ADR-0013 §1). That framing decides the design:

* every write is idempotent, because the same events may be replayed into a fresh
  database after a rebuild;
* nothing here is a source of truth, so a mismatch between the database and the journal is
  a detectable bug rather than a fork;
* the domain depends on :class:`Repository`, never on a driver. The offline fast lane uses
  :class:`InMemoryRepository`; deployment uses
  :class:`~tigerquant.db.postgres.PostgresRepository`.

Behaviour is specified once, in ``tests/unit/test_db_repository.py``, and that same suite is
run against both implementations — the Postgres half under the ``requires_db`` marker, so a
green offline run never implies the SQL was exercised.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tigerquant.core.clock import ensure_utc
from tigerquant.core.errors import JournalIntegrityError

if TYPE_CHECKING:
    from tigerquant.events.base import EventBase
    from tigerquant.evidence.facts import Fact
    from tigerquant.execution.intent import OrderSnapshot
    from tigerquant.portfolio.ledger import Position

__all__ = [
    "AlertRow",
    "DecisionRow",
    "InMemoryRepository",
    "PortfolioStateRow",
    "Repository",
    "RepositoryHealth",
    "RiskOutcomeRow",
]


@dataclass(frozen=True, slots=True)
class RepositoryHealth:
    """Whether the projection store is usable, and why not when it is not."""

    reachable: bool
    detail: str
    schema_version: int = 0

    def __bool__(self) -> bool:
        return self.reachable


@dataclass(frozen=True, slots=True)
class AlertRow:
    """A persisted operator alert."""

    seq: int
    severity: str
    code: str
    message: str
    occurrences: int
    occurred_at: dt.datetime


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """A persisted decision record.

    Mirrors the ``decisions`` table exactly so the Postgres insert has no invented fields
    and no silently dropped ones.
    """

    decision_id: str
    snapshot_id: str
    model_version: str
    prompt_version: str
    action: str
    instrument: str | None
    target_weight: object | None
    horizon: str
    thesis: str
    counter_thesis: str
    invalidation: str
    evidence_ids: tuple[str, ...]
    uncertainties: tuple[str, ...]
    occurred_at: dt.datetime


@dataclass(frozen=True, slots=True)
class RiskOutcomeRow:
    """A persisted risk verdict."""

    seq: int
    intent_id: str
    outcome: str
    reason_codes: tuple[str, ...]
    policy_hash: str
    portfolio_digest: str
    checks_run: tuple[str, ...]
    detail: str
    occurred_at: dt.datetime


@dataclass(frozen=True, slots=True)
class PortfolioStateRow:
    """Current cash and valuation."""

    cash: object
    equity: object
    gross_exposure: object
    net_exposure: object
    ledger_digest: str
    updated_seq: int
    as_of: dt.datetime


@runtime_checkable
class Repository(Protocol):
    """What the domain requires of projection storage."""

    def health(self) -> RepositoryHealth: ...

    # -- events ------------------------------------------------------------------
    def append_event(self, event: EventBase) -> bool:
        """Store an event. Returns ``False`` when ``event_id`` was already present."""
        ...

    def events(self, *, limit: int | None = None) -> tuple[EventBase, ...]: ...

    def event_count(self) -> int: ...

    # -- orders and fills --------------------------------------------------------
    def upsert_order(self, snapshot: OrderSnapshot, *, seq: int) -> None: ...

    def order(self, intent_id: str) -> OrderSnapshot | None: ...

    def open_orders(self) -> tuple[OrderSnapshot, ...]: ...

    def append_fill(
        self, *, exec_id: str, intent_id: str, seq: int, payload: dict[str, object]
    ) -> bool:
        """Record a fill. Returns ``False`` when ``exec_id`` was already booked."""
        ...

    def fill_count(self, intent_id: str) -> int: ...

    # -- portfolio ---------------------------------------------------------------
    def upsert_position(self, position: Position, *, seq: int) -> None: ...

    def positions(self) -> tuple[Position, ...]: ...

    def set_portfolio_state(self, state: PortfolioStateRow, *, seq: int) -> None: ...

    def portfolio_state(self) -> PortfolioStateRow | None: ...

    # -- evidence ----------------------------------------------------------------
    def add_fact(self, fact: Fact) -> bool: ...

    def facts_as_of(
        self, instant: dt.datetime, *, category: str | None = None
    ) -> tuple[Fact, ...]: ...

    # -- lineage -----------------------------------------------------------------
    def record_decision(self, row: DecisionRow, *, seq: int) -> None: ...

    def record_risk_outcome(self, row: RiskOutcomeRow) -> None: ...

    def add_alert(self, row: AlertRow) -> None: ...

    def alerts(self, *, limit: int = 100) -> tuple[AlertRow, ...]: ...


class InMemoryRepository:
    """The offline implementation.

    Not a stub: it enforces the same invariants as the SQL (idempotent event and fill
    insertion, long-only positions, append-only events) so that the contract suite is
    meaningful, and so the fast lane exercises the real projection logic without a server.
    """

    __slots__ = (
        "_alerts",
        "_decisions",
        "_event_ids",
        "_events",
        "_facts",
        "_fills",
        "_orders",
        "_portfolio",
        "_positions",
        "_reachable",
        "_risk_outcomes",
        "health_calls",
    )

    def __init__(self) -> None:
        self._events: dict[int, EventBase] = {}
        self._event_ids: set[str] = set()
        self._orders: dict[str, OrderSnapshot] = {}
        self._fills: dict[str, dict[str, object]] = {}
        self._positions: dict[str, Position] = {}
        self._portfolio: PortfolioStateRow | None = None
        self._facts: dict[str, Fact] = {}
        self._decisions: dict[str, DecisionRow] = {}
        self._risk_outcomes: dict[int, RiskOutcomeRow] = {}
        self._alerts: dict[int, AlertRow] = {}
        self.health_calls = 0
        self._reachable = True

    # -- health ------------------------------------------------------------------

    def health(self) -> RepositoryHealth:
        self.health_calls += 1
        return RepositoryHealth(
            reachable=self._reachable,
            detail="in-memory projection store",
            schema_version=1,
        )

    def simulate_outage(self, *, reachable: bool) -> None:
        """Test hook: make the store behave as if the server were unreachable."""
        self._reachable = reachable

    def _require_reachable(self) -> None:
        if not self._reachable:
            msg = "projection store is unreachable"
            raise JournalIntegrityError(msg)

    # -- events ------------------------------------------------------------------

    def append_event(self, event: EventBase) -> bool:
        self._require_reachable()
        if event.event_id in self._event_ids:
            return False
        if event.seq in self._events:
            msg = (
                f"event sequence {event.seq} is already occupied by "
                f"{self._events[event.seq].event_type}; the projection store must not fork"
            )
            raise JournalIntegrityError(msg)
        self._events[event.seq] = event
        self._event_ids.add(event.event_id)
        return True

    def events(self, *, limit: int | None = None) -> tuple[EventBase, ...]:
        ordered = tuple(self._events[key] for key in sorted(self._events))
        if limit is None:
            return ordered
        return ordered[-limit:]

    def event_count(self) -> int:
        return len(self._events)

    # -- orders and fills --------------------------------------------------------

    def upsert_order(self, snapshot: OrderSnapshot, *, seq: int) -> None:
        self._require_reachable()
        del seq  # the in-memory store has no need to track the high-water mark
        existing = self._orders.get(snapshot.intent_id)
        if existing is not None and existing.status in {"FILLED", "CANCELED", "REJECTED"}:
            if existing.status != snapshot.status and snapshot.status not in {
                "QUARANTINED",
            }:
                msg = (
                    f"order {snapshot.intent_id} is terminal ({existing.status}) and cannot "
                    f"move to {snapshot.status}"
                )
                raise JournalIntegrityError(msg)
        self._orders[snapshot.intent_id] = snapshot

    def order(self, intent_id: str) -> OrderSnapshot | None:
        return self._orders.get(intent_id)

    def open_orders(self) -> tuple[OrderSnapshot, ...]:
        return tuple(
            snapshot
            for _, snapshot in sorted(self._orders.items())
            if snapshot.status not in {"FILLED", "CANCELED", "REJECTED"}
        )

    def append_fill(
        self, *, exec_id: str, intent_id: str, seq: int, payload: dict[str, object]
    ) -> bool:
        self._require_reachable()
        if exec_id in self._fills:
            return False
        self._fills[exec_id] = {"intent_id": intent_id, "seq": seq, **payload}
        return True

    def fill_count(self, intent_id: str) -> int:
        return sum(1 for row in self._fills.values() if row.get("intent_id") == intent_id)

    # -- portfolio ---------------------------------------------------------------

    def upsert_position(self, position: Position, *, seq: int) -> None:
        self._require_reachable()
        del seq
        if position.quantity < 0:
            msg = (
                f"refusing to store a short position in {position.instrument} "
                f"(quantity {position.quantity}); this system is long-only"
            )
            raise JournalIntegrityError(msg)
        self._positions[position.instrument] = position

    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions[key] for key in sorted(self._positions))

    def set_portfolio_state(self, state: PortfolioStateRow, *, seq: int) -> None:
        self._require_reachable()
        del seq
        self._portfolio = state

    def portfolio_state(self) -> PortfolioStateRow | None:
        return self._portfolio

    # -- evidence ----------------------------------------------------------------

    def add_fact(self, fact: Fact) -> bool:
        self._require_reachable()
        if fact.fact_id in self._facts:
            return False
        self._facts[fact.fact_id] = fact
        return True

    def facts_as_of(self, instant: dt.datetime, *, category: str | None = None) -> tuple[Fact, ...]:
        moment = ensure_utc(instant)
        return tuple(
            fact
            for fact in self._facts.values()
            if fact.available_at <= moment and (category is None or fact.category == category)
        )

    # -- lineage -----------------------------------------------------------------

    def record_decision(self, row: DecisionRow, *, seq: int) -> None:
        self._require_reachable()
        del seq
        self._decisions.setdefault(row.decision_id, row)

    def record_risk_outcome(self, row: RiskOutcomeRow) -> None:
        self._require_reachable()
        if row.outcome == "DENY" and not row.reason_codes:
            msg = f"risk outcome {row.seq} is a DENY with no reason code"
            raise JournalIntegrityError(msg)
        self._risk_outcomes.setdefault(row.seq, row)

    def add_alert(self, row: AlertRow) -> None:
        self._require_reachable()
        self._alerts.setdefault(row.seq, row)

    def alerts(self, *, limit: int = 100) -> tuple[AlertRow, ...]:
        ordered = [self._alerts[key] for key in sorted(self._alerts)]
        return tuple(ordered[-limit:])

    # -- inspection helpers used by tests ----------------------------------------

    def decisions(self) -> tuple[DecisionRow, ...]:
        return tuple(self._decisions[key] for key in sorted(self._decisions))

    def risk_outcomes(self) -> tuple[RiskOutcomeRow, ...]:
        return tuple(self._risk_outcomes[key] for key in sorted(self._risk_outcomes))

    def fill_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._fills))
