"""Project the audit journal into PostgreSQL, and check the two for drift.

ADR-0013 fixes the precedence: the hash-chained JSONL journal is authoritative, and the
database holds *rebuildable* projections. This module is what makes that claim true rather
than aspirational — it reconstructs the relational tables from the journal alone, and it
compares what the journal implies against what the database currently says.

Two operations, deliberately separate:

:func:`project` folds journal events into a repository. It is **idempotent**: every write is
keyed by the event that produced it, so replaying an already-applied journal changes nothing.
That matters because the honest answer to "did this batch get applied?" is usually "I don't
know" — a crash can lose the acknowledgement without losing the work.

:func:`check_drift` compares the journal against the database and reports differences. It
does **not** repair them. A divergence means we do not know which record is wrong, and
silently overwriting one with the other would destroy exactly the evidence needed to find
out. The caller decides; the default is to stop and tell a human (ADR-0004 §5).

The order fold is **not** reimplemented here. :class:`~tigerquant.execution.intent.OrderProjection`
already owns the order state machine, and a second implementation of it would be a second
place for the state machine to be wrong. This module folds with that projection and then
persists what it produced.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import digest_of
from tigerquant.core.enums import Mode
from tigerquant.core.errors import JournalIntegrityError
from tigerquant.core.money import ZERO, q_cash
from tigerquant.db.repository import PortfolioStateRow
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.execution.intent import OrderProjection

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tigerquant.db.repository import Repository
    from tigerquant.events.base import EventBase

__all__ = [
    "DriftReport",
    "ProjectionReport",
    "check_drift",
    "project",
    "project_journal_file",
]

#: Event types that can change an order's folded state.
#:
#: RiskEvaluated is included even though its name says nothing about orders: the order state
#: machine requires CREATED -> RISK_APPROVED -> SUBMITTING, and RiskEvaluated is the event
#: that carries that transition. Omitting it made the fold fail on the first submission with
#: an illegal-transition error, which is exactly the failure a second implementation of the
#: state machine would have hidden.
_ORDER_EVENTS = frozenset(
    {
        "OrderIntentCreated",
        "RiskEvaluated",
        "OrderSubmitted",
        "OrderAcknowledged",
        "OrderPartiallyFilled",
        "OrderFilled",
        "OrderCanceled",
        "OrderRejected",
        "OrderSubmissionUnknown",
        "OrderQuarantined",
        "OrderCancelRequested",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectionReport:
    """What a projection pass applied."""

    events_seen: int
    events_applied: int
    events_already_present: int
    order_rows: int
    orders_tracked: int
    orders_open: int
    portfolio_marks: int
    last_sequence: int

    def digest(self) -> str:
        return digest_of(
            {
                "events_seen": self.events_seen,
                "events_applied": self.events_applied,
                "order_rows": self.order_rows,
                "portfolio_marks": self.portfolio_marks,
                "last_sequence": self.last_sequence,
            }
        )

    def describe(self) -> str:
        already = (
            f"; {self.events_already_present} already present"
            if self.events_already_present
            else ""
        )
        return (
            f"projected {self.events_applied}/{self.events_seen} event(s){already}: "
            f"{self.order_rows} order row(s) written, {self.orders_tracked} order(s) tracked "
            f"({self.orders_open} open), {self.portfolio_marks} portfolio mark(s); "
            f"last sequence {self.last_sequence}"
        )


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Where the journal and the database disagree."""

    journal_events: int
    database_events: int
    missing_event_ids: tuple[str, ...]
    unexpected_event_ids: tuple[str, ...]
    journal_orders: int
    database_orders: int
    journal_cash: Decimal | None
    database_cash: Decimal | None
    notes: tuple[str, ...] = ()

    @property
    def is_consistent(self) -> bool:
        cash_differs = (
            self.journal_cash is not None
            and self.database_cash is not None
            and self.journal_cash != self.database_cash
        )
        return not (
            self.missing_event_ids
            or self.unexpected_event_ids
            or self.journal_orders != self.database_orders
            or cash_differs
        )

    def summary(self) -> str:
        if self.is_consistent:
            return (
                f"consistent: {self.journal_events} event(s), {self.journal_orders} order(s), "
                f"cash {self.journal_cash}"
            )
        parts: list[str] = []
        if self.missing_event_ids:
            parts.append(f"{len(self.missing_event_ids)} journal event(s) absent from the database")
        if self.unexpected_event_ids:
            parts.append(
                f"{len(self.unexpected_event_ids)} database event(s) absent from the journal "
                "(the database holds history the journal does not — the more alarming "
                "direction, because the journal is the record of authority)"
            )
        if self.journal_orders != self.database_orders:
            parts.append(f"orders: journal {self.journal_orders}, database {self.database_orders}")
        if (
            self.journal_cash is not None
            and self.database_cash is not None
            and self.journal_cash != self.database_cash
        ):
            parts.append(f"cash: journal {self.journal_cash}, database {self.database_cash}")
        return "DRIFT — " + "; ".join(parts) + ". Not repaired automatically."

    def as_record(self) -> dict[str, object]:
        return {
            "consistent": self.is_consistent,
            "journal_events": self.journal_events,
            "database_events": self.database_events,
            "missing_event_ids": list(self.missing_event_ids),
            "unexpected_event_ids": list(self.unexpected_event_ids),
            "journal_orders": self.journal_orders,
            "database_orders": self.database_orders,
            "journal_cash": None if self.journal_cash is None else str(self.journal_cash),
            "database_cash": None if self.database_cash is None else str(self.database_cash),
            "notes": list(self.notes),
        }


def project(
    events: Iterable[EventBase],
    repository: Repository,
    *,
    require_contiguous_sequence: bool = True,
) -> ProjectionReport:
    """Fold journal events into the repository. Idempotent.

    ``require_contiguous_sequence`` refuses a journal with a gap. A gap means events were
    lost, and projecting a gapped journal would write a projection that is confidently
    wrong — worse than one that is obviously incomplete.
    """
    projection = OrderProjection()
    seen = 0
    applied = 0
    already_present = 0
    order_rows = 0
    marks = 0
    last_sequence = 0
    expected_sequence: int | None = None

    for event in events:
        seen += 1
        sequence = event.seq
        if (
            require_contiguous_sequence
            and expected_sequence is not None
            and sequence > expected_sequence + 1
        ):
            msg = (
                f"journal has a gap: sequence {expected_sequence} is followed by {sequence}. "
                "Events were lost, so no projection built from it can be trusted."
            )
            raise JournalIntegrityError(msg)
        expected_sequence = sequence
        last_sequence = max(last_sequence, sequence)

        if repository.append_event(event):
            appended = True
            applied += 1
        else:
            appended = False
            already_present += 1

        kind = type(event).__name__
        if kind in _ORDER_EVENTS:
            # Always fold locally: a later event in this same pass needs the prior state to be
            # correct even when its own write is skipped.
            projection.apply(event)
            intent_id = str(_payload(event).get("intent_id", ""))
            snapshot = projection.get(intent_id)
            if snapshot is not None and (appended or repository.order(intent_id) is None):
                # Write only when this event is new, or when the row is genuinely missing (a
                # rebuild after losing the projections).
                #
                # Writing unconditionally was a real bug. On a second pass the fold starts
                # from a fresh OrderProjection, so the run's first event re-emits CREATED and
                # would regress an order the venue had already filled back to CREATED. The
                # repository's terminal-state guard caught it; the fix is that replaying must
                # be a no-op.
                repository.upsert_order(snapshot, seq=sequence)
                if appended:
                    order_rows += 1
        elif kind == "LedgerMarked":
            # The same rule for valuations: replaying must not move the mark backwards.
            if appended or repository.portfolio_state() is None:
                repository.set_portfolio_state(_portfolio_row(event), seq=sequence)
                marks += 1

    return ProjectionReport(
        events_seen=seen,
        events_applied=applied,
        events_already_present=already_present,
        order_rows=order_rows,
        orders_tracked=len(projection.snapshots()),
        orders_open=len(projection.open_intents()),
        portfolio_marks=marks,
        last_sequence=last_sequence,
    )


def _payload(event: EventBase) -> dict[str, Any]:
    """The event's payload mapping.

    `EventBase.payload` is a *method*, not an attribute, so the natural-looking
    `getattr(event, "payload", {})` silently returns a bound method rather than a
    mapping and fails much later with a confusing error. This helper is the single place
    that knows the difference.
    """
    raw = getattr(event, "payload", None)
    if callable(raw):
        raw = raw()
    return dict(raw or {})


def _portfolio_row(event: EventBase) -> PortfolioStateRow:
    payload = _payload(event)
    return PortfolioStateRow(
        cash=q_cash(_dec(payload.get("cash"))),
        equity=q_cash(_dec(payload.get("equity"))),
        gross_exposure=q_cash(_dec(payload.get("gross_exposure"))),
        net_exposure=q_cash(_dec(payload.get("net_exposure"))),
        ledger_digest=str(payload.get("ledger_digest", "")),
        updated_seq=event.seq,
        as_of=_as_datetime(payload.get("as_of")) or event.occurred_at,
    )


def _dec(value: object) -> Decimal:
    return ZERO if value is None else Decimal(str(value))


def _as_datetime(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def project_journal_file(
    path: Path | str, repository: Repository, *, mode: Mode = Mode.BACKTEST
) -> ProjectionReport:
    """Read a JSONL journal file and project it into a repository.

    Opening through :class:AppendOnlyJournal rather than reading the lines directly means
    the hash chain is verified on the way in. A corrupt journal must not become a
    confidently wrong projection.
    """
    with AppendOnlyJournal(Path(path), mode, fsync=False) as journal:
        return project((record.event for record in journal.iter_records()), repository)


def check_drift(
    journal_events: Sequence[EventBase],
    repository: Repository,
) -> DriftReport:
    """Compare the journal with the database. Reports differences; never repairs them."""
    journal_by_id = {event.event_id: event for event in journal_events}
    database_events = repository.events()
    database_by_id = {event.event_id: event for event in database_events}

    missing = tuple(sorted(set(journal_by_id) - set(database_by_id)))
    unexpected = tuple(sorted(set(database_by_id) - set(journal_by_id)))

    journal_intent_ids = _order_intent_ids(journal_events)
    # Every order the database knows about, not only the open ones: open_orders() alone
    # would make a fully filled order look like drift, because a completed order is not open.
    database_intent_ids = {
        snapshot.intent_id
        for intent_id in journal_intent_ids
        if (snapshot := repository.order(intent_id)) is not None
    }

    journal_cash: Decimal | None = None
    for event in journal_events:
        if type(event).__name__ == "LedgerMarked":
            journal_cash = q_cash(_dec(_payload(event).get("cash")))
    state = repository.portfolio_state()

    notes: list[str] = []
    if unexpected:
        notes.append(
            "the database holds events the journal does not. Since the journal is the record "
            "of authority, this means either a journal file was truncated or lost, or "
            "something wrote to the database directly. Treat the journal as damaged until "
            "proven otherwise."
        )

    return DriftReport(
        journal_events=len(journal_events),
        database_events=len(database_events),
        missing_event_ids=missing,
        unexpected_event_ids=unexpected,
        journal_orders=len(journal_intent_ids),
        database_orders=len(database_intent_ids),
        journal_cash=journal_cash,
        database_cash=None if state is None else q_cash(_dec(state.cash)),
        notes=tuple(notes),
    )


def _order_intent_ids(events: Sequence[EventBase]) -> set[str]:
    found = {
        str(_payload(event).get("intent_id", ""))
        for event in events
        if type(event).__name__ in _ORDER_EVENTS
    }
    found.discard("")
    return found
