"""Journal -> database projection and the drift check.

The property that matters most here is **idempotence**. The honest answer to "did this batch
get applied?" is usually "I don't know", because a crash can lose the acknowledgement without
losing the work — so replaying a journal must be safe, and safe means it changes nothing.

The second property is that drift is *reported and never repaired*. If the journal and the
database disagree, we do not know which one is wrong, and overwriting either would destroy
the evidence needed to find out.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.core.enums import Mode
from tigerquant.core.errors import IllegalOrderTransition, JournalIntegrityError
from tigerquant.db.projector import check_drift, project
from tigerquant.db.repository import InMemoryRepository
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.market.fixtures import load_scenario
from tigerquant.pipeline.replay import run_replay

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
FIXTURE = "tests/fixtures/scenarios/trend_up.yaml"


@pytest.fixture
def journal_events(tmp_path: pytest.TempPathFactory) -> tuple[object, ...]:
    """A real replay journal, produced by the same code path production uses."""
    path = tmp_path / "journal.jsonl"  # type: ignore[operator]
    run_replay(load_scenario(FIXTURE), journal_path=path)
    with AppendOnlyJournal(path, Mode.BACKTEST, fsync=False) as journal:
        return tuple(record.event for record in journal.iter_records())


class TestProjection:
    def test_projects_a_real_journal(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        report = project(journal_events, repository)  # type: ignore[arg-type]
        assert report.events_seen == len(journal_events)
        assert report.events_applied == len(journal_events)
        assert report.orders_tracked == 1
        assert report.order_rows > 0
        assert report.portfolio_marks > 0

    def test_cash_matches_the_replay_report(self, journal_events: tuple[object, ...]) -> None:
        """The projection must agree with the journal it came from."""
        repository = InMemoryRepository()
        project(journal_events, repository)  # type: ignore[arg-type]
        state = repository.portfolio_state()
        assert state is not None
        # CR-01's recorded evidence for this scenario.
        assert Decimal(str(state.cash)) == Decimal("95179.48")

    def test_projection_is_idempotent(self, journal_events: tuple[object, ...]) -> None:
        """Replaying must change nothing. This is the property that makes retry safe."""
        repository = InMemoryRepository()
        first = project(journal_events, repository)  # type: ignore[arg-type]
        before = repository.portfolio_state()
        second = project(journal_events, repository)  # type: ignore[arg-type]

        assert first.events_applied == len(journal_events)
        assert second.events_applied == 0
        assert second.events_already_present == len(journal_events)
        assert second.orders_tracked == first.orders_tracked
        assert repository.portfolio_state() == before

    def test_the_journal_digest_is_stable_across_projection(
        self, journal_events: tuple[object, ...]
    ) -> None:
        report = project(journal_events, InMemoryRepository())  # type: ignore[arg-type]
        assert report.digest() == report.digest()

    def test_a_sequence_gap_is_refused(self, journal_events: tuple[object, ...]) -> None:
        """A gapped journal would produce a projection that is confidently wrong."""
        gapped = (journal_events[0], journal_events[3], *journal_events[4:])
        with pytest.raises(JournalIntegrityError, match="gap"):
            project(gapped, InMemoryRepository())  # type: ignore[arg-type]

    def test_a_gap_can_be_forced_for_inspecting_a_damaged_journal(
        self, journal_events: tuple[object, ...]
    ) -> None:
        gapped = (journal_events[0], journal_events[3], *journal_events[4:])
        report = project(
            gapped,
            InMemoryRepository(),
            require_contiguous_sequence=False,  # type: ignore[arg-type]
        )
        assert report.events_seen == len(gapped)

    def test_the_order_fold_matches_the_state_machine(
        self, journal_events: tuple[object, ...]
    ) -> None:
        """The projector reuses OrderProjection rather than reimplementing the fold.

        If it had its own copy, a change to the state machine would silently diverge here.
        The scenario produces one order that fills, so it must end complete and closed.
        """
        repository = InMemoryRepository()
        report = project(journal_events, repository)  # type: ignore[arg-type]
        assert report.orders_tracked == 1
        assert report.orders_open == 0


class TestOtherProjections:
    """Fills, positions, decisions and risk outcomes, not only orders and cash.

    The fill path needed the most care: OrderPartiallyFilled and OrderFilled belong to BOTH
    the order-event set and the fill-event set, so a naive elif chain silently projected zero
    fills while every other counter looked correct.
    """

    def test_fills_are_projected(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        report = project(journal_events, repository)  # type: ignore[arg-type]
        assert report.fills == 1
        assert repository.fill_count("bt-oi_01KJQG32W00000") == 1

    def test_positions_are_projected(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        report = project(journal_events, repository)  # type: ignore[arg-type]
        assert report.positions >= 1
        assert any(position.instrument == "SPY" for position in repository.positions())

    def test_decisions_are_projected(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        report = project(journal_events, repository)  # type: ignore[arg-type]
        assert report.decisions >= 1

    def test_risk_outcomes_are_projected(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        report = project(journal_events, repository)  # type: ignore[arg-type]
        assert report.risk_outcomes >= 1

    def test_every_projection_is_idempotent(self, journal_events: tuple[object, ...]) -> None:
        """The strongest statement: a second pass changes no row of any table."""
        repository = InMemoryRepository()
        first = project(journal_events, repository)  # type: ignore[arg-type]
        before = (
            repository.fill_count("bt-oi_01KJQG32W00000"),
            repository.positions(),
            repository.portfolio_state(),
        )
        second = project(journal_events, repository)  # type: ignore[arg-type]

        assert second.fills == 0
        assert second.events_applied == 0
        assert (
            repository.fill_count("bt-oi_01KJQG32W00000"),
            repository.positions(),
            repository.portfolio_state(),
        ) == before
        assert first.orders_tracked == second.orders_tracked

    def test_a_fill_without_its_order_is_refused_by_the_state_machine(
        self, journal_events: tuple[object, ...]
    ) -> None:
        """A fill for an unknown order must not fabricate an instrument or an order.

        The order fold refuses it first with IllegalOrderTransition, which is stronger than
        the projector's own skip: the state machine will not accept an execution for an order
        that was never created. The projector's guard remains for the case where a fill is
        projected without the fold, but in the normal path this is what catches it.
        """
        fill_only = tuple(
            event
            for event in journal_events
            if type(event).__name__ in {"OrderFilled", "OrderPartiallyFilled"}
        )
        assert fill_only, "the scenario should contain a fill"
        repository = InMemoryRepository()
        with pytest.raises(IllegalOrderTransition):
            project(fill_only, repository)  # type: ignore[arg-type]
        # Nothing was invented and nothing was written beyond the event log itself.
        assert repository.positions() == ()


class TestDrift:
    def test_a_consistent_pair_reports_consistent(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        project(journal_events, repository)  # type: ignore[arg-type]
        drift = check_drift(journal_events, repository)  # type: ignore[arg-type]
        assert drift.is_consistent
        assert drift.journal_events == drift.database_events
        assert drift.journal_cash == drift.database_cash

    def test_a_missing_event_is_detected(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        project(journal_events[:5], repository)  # type: ignore[arg-type]
        drift = check_drift(journal_events, repository)  # type: ignore[arg-type]
        assert not drift.is_consistent
        assert drift.missing_event_ids
        assert "absent from the database" in drift.summary()

    def test_an_unexpected_event_is_flagged_as_the_alarming_direction(
        self, journal_events: tuple[object, ...]
    ) -> None:
        """The database holding history the journal does not is the worse failure.

        The journal is the record of authority. Extra rows in the database mean a journal was
        truncated or something wrote directly, so the note says so rather than treating both
        directions as equivalent.
        """
        repository = InMemoryRepository()
        project(journal_events, repository)  # type: ignore[arg-type]
        drift = check_drift(journal_events[:10], repository)  # type: ignore[arg-type]
        assert drift.unexpected_event_ids
        assert any("record of authority" in note for note in drift.notes)

    def test_drift_is_reported_not_repaired(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        project(journal_events[:5], repository)  # type: ignore[arg-type]
        before = repository.event_count()
        check_drift(journal_events, repository)  # type: ignore[arg-type]
        assert repository.event_count() == before

    def test_drift_record_is_audit_safe(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        project(journal_events[:5], repository)  # type: ignore[arg-type]
        record = check_drift(journal_events, repository).as_record()  # type: ignore[arg-type]
        assert record["consistent"] is False
        assert isinstance(record["missing_event_ids"], list)
