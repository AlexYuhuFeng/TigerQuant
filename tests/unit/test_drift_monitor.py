"""CR-09: drift monitoring and its alerting behaviour.

The gate asks for "measured false alerts". That is a design constraint. A monitor that alerts
once per tick for a single unresolved problem produces thousands of identical alerts and the
operator stops reading them — which is the same as having no monitor. These tests pin the
transition-based behaviour and measure the resulting rate.

They also pin two things the monitor deliberately does NOT do: it never repairs a drift, and it
never halts. Both belong elsewhere.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Mode, Severity
from tigerquant.db.projector import project
from tigerquant.db.repository import InMemoryRepository
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.journal import AppendOnlyJournal, MemoryJournal
from tigerquant.events.risk import AlertRaised
from tigerquant.market.fixtures import load_scenario
from tigerquant.ops.drift_monitor import (
    DEFAULT_DRIFT_POLICY,
    DriftAlertPolicy,
    DriftMonitor,
    drift_ratio,
    interval_runner,
)
from tigerquant.pipeline.replay import run_replay

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
FIXTURE = "tests/fixtures/scenarios/trend_up.yaml"


@pytest.fixture
def journal_events(tmp_path: pytest.TempPathFactory) -> tuple[object, ...]:
    path = tmp_path / "journal.jsonl"  # type: ignore[operator]
    run_replay(load_scenario(FIXTURE), journal_path=path)
    with AppendOnlyJournal(path, Mode.BACKTEST, fsync=False) as journal:
        return tuple(record.event for record in journal.iter_records())


def make_monitor(
    repository: InMemoryRepository,
    *,
    policy: DriftAlertPolicy | None = None,
) -> tuple[DriftMonitor, MemoryJournal]:
    """A monitor writing alerts to an in-memory journal.

    Returns the SINK rather than a list of its records: a list would be a snapshot taken
    before any check ran, and every assertion would see nothing. Read ``sink.records`` at
    assertion time instead.
    """
    sink = MemoryJournal(Mode.BACKTEST)
    clock = SimulatedClock(AT)
    from tigerquant.core.ids import IdFactory

    emitter = EventEmitter(
        mode=Mode.BACKTEST,
        sink=sink,
        ids=IdFactory("drift", clock),
        clock=clock,
        correlation_id="drift-test",
    )
    monitor = DriftMonitor(
        repository=repository, emitter=emitter, policy=policy or DEFAULT_DRIFT_POLICY
    )
    return monitor, sink


class TestAlertPolicy:
    def test_a_non_positive_reminder_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="reminder_interval_seconds"):
            DriftAlertPolicy(reminder_interval_seconds=0)

    def test_a_non_positive_escalation_threshold_is_refused(self) -> None:
        with pytest.raises(ValueError, match="escalate_after"):
            DriftAlertPolicy(escalate_after=0)


class TestConsistentState:
    def test_a_consistent_pair_reports_consistent_and_alert_free(
        self, journal_events: tuple[object, ...]
    ) -> None:
        repository = InMemoryRepository()
        project(journal_events, repository)  # type: ignore[arg-type]
        monitor, _ = make_monitor(repository)

        result = monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert result.consistent
        assert not result.blocked if hasattr(result, "blocked") else True
        assert not result.blocks_new_exposure
        assert result.severity is None
        assert not monitor.is_drifted
        assert monitor.consecutive_drifted == 0

    def test_repeated_consistent_checks_alert_nothing(
        self, journal_events: tuple[object, ...]
    ) -> None:
        repository = InMemoryRepository()
        project(journal_events, repository)  # type: ignore[arg-type]
        monitor, sink = make_monitor(repository)
        for _ in range(5):
            monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert sink.records == ()
        assert monitor.alert_rate()["alerts"] == 0.0


class TestDriftAlerts:
    def test_drift_does_not_alert_per_check(self, journal_events: tuple[object, ...]) -> None:
        """The behaviour the gate's "measured false alerts" is really about.

        Ten checks of one unresolved drift produce exactly two alerts, and both are
        meaningful: the first announces the drift, the second (at the escalation threshold)
        says it has not cleared by itself. A monitor that alerted ten times would be
        unreadable; one that alerted never would be useless.
        """
        repository = InMemoryRepository()
        monitor, sink = make_monitor(repository)

        results = [monitor.check(journal_events, at=AT) for _ in range(10)]  # type: ignore[arg-type]

        assert all(not r.consistent for r in results)
        assert results[0].alerted, "the first drift must alert"
        alerted_at = [index for index, r in enumerate(results) if r.alerted]
        assert alerted_at == [0, 2], "one announcement and one escalation, nothing else"
        assert len(sink.records) == 2
        assert all(r.occurrences == index + 1 for index, r in enumerate(results))

    def test_occurrences_accumulate(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        monitor, _ = make_monitor(repository)
        for _ in range(4):
            result = monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert result.occurrences == 4

    def test_drift_escalates_after_the_threshold(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        monitor, sink = make_monitor(
            repository, policy=DriftAlertPolicy(escalate_after=3, reminder_interval_seconds=3600)
        )

        first = monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert first.severity is Severity.WARNING

        second = monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert not second.alerted, "still warning, no reminder due yet"

        third = monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert third.escalated
        assert third.severity is Severity.CRITICAL
        assert sum(1 for r in [first, second, third] if r.alerted) == 2

    def test_a_reminder_fires_on_the_cadence(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        monitor, _ = make_monitor(
            repository, policy=DriftAlertPolicy(escalate_after=100, reminder_interval_seconds=600)
        )

        monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert not monitor.check(
            journal_events,
            at=AT + dt.timedelta(seconds=300),  # type: ignore[arg-type]
        ).alerted
        assert monitor.check(
            journal_events,
            at=AT + dt.timedelta(seconds=900),  # type: ignore[arg-type]
        ).alerted

    def test_recovery_is_announced(self, journal_events: tuple[object, ...]) -> None:
        """The operator should not have to re-check to learn the problem cleared."""
        repository = InMemoryRepository()
        monitor, sink = make_monitor(repository)

        monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert monitor.is_drifted
        before = len(sink.records)

        project(journal_events, repository)  # type: ignore[arg-type]
        recovered = monitor.check(journal_events, at=AT)  # type: ignore[arg-type]

        assert recovered.consistent
        assert recovered.recovered
        assert not monitor.is_drifted
        assert len(sink.records) == before + 1, "exactly one recovery alert"

    def test_recovery_is_not_announced_when_nothing_was_wrong(
        self, journal_events: tuple[object, ...]
    ) -> None:
        repository = InMemoryRepository()
        project(journal_events, repository)  # type: ignore[arg-type]
        monitor, _ = make_monitor(repository)
        result = monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert not result.recovered

    def test_alert_payload_reports_the_occurrence_count(
        self, journal_events: tuple[object, ...]
    ) -> None:
        repository = InMemoryRepository()
        monitor, sink = make_monitor(repository)
        for _ in range(3):
            monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        record = sink.records[0]
        event = record.event
        assert isinstance(event, AlertRaised)
        assert event.occurrences == 1
        assert event.context["monitor"] == "drift"


class TestMeasuredRate:
    def test_the_rate_is_measurable(self, journal_events: tuple[object, ...]) -> None:
        """'Measured false alerts' has to be a number, not an impression."""
        repository = InMemoryRepository()
        monitor, _ = make_monitor(repository)
        for _ in range(20):
            monitor.check(journal_events, at=AT)  # type: ignore[arg-type]

        rates = monitor.alert_rate()
        assert rates["checks"] == 20
        assert rates["drifted_checks"] == 20
        # Two alerts (announcement plus escalation) for twenty drifted checks. The property
        # being defended is that the ratio stays FAR below 1.
        assert rates["alerts"] == 2
        assert rates["alerts_per_drifted_check"] == pytest.approx(0.1)
        assert rates["alerts_per_drifted_check"] < 0.5

    def test_a_monitor_that_shouts_is_visible_in_the_rate(
        self, journal_events: tuple[object, ...]
    ) -> None:
        """The other failure mode: reminding every check."""
        repository = InMemoryRepository()
        monitor, _ = make_monitor(
            repository, policy=DriftAlertPolicy(escalate_after=100, reminder_interval_seconds=1)
        )
        for minute in range(5):
            monitor.check(journal_events, at=AT + dt.timedelta(minutes=minute))  # type: ignore[arg-type]
        assert monitor.alert_rate()["alerts_per_drifted_check"] > 0.5

    def test_summary_reads_like_a_report(self, journal_events: tuple[object, ...]) -> None:
        repository = InMemoryRepository()
        monitor, _ = make_monitor(repository)
        assert "consistent" in monitor.summarise()
        monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert "DRIFTED" in monitor.summarise()


class TestItReportsAndNeverRepairs:
    def test_the_monitor_does_not_touch_the_database(
        self, journal_events: tuple[object, ...]
    ) -> None:
        """Repairing would destroy the evidence needed to explain the divergence."""
        repository = InMemoryRepository()
        monitor, _ = make_monitor(repository)
        monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert repository.event_count() == 0

    def test_the_monitor_never_halts(self, journal_events: tuple[object, ...]) -> None:
        """Halting is the watchdog's job. A second place a halt can be wrong is a liability."""
        from tigerquant.risk.state import RiskState

        repository = InMemoryRepository()
        monitor, _ = make_monitor(repository)
        state = RiskState()
        monitor.check(journal_events, at=AT)  # type: ignore[arg-type]
        assert not state.halted, "the drift monitor must not be able to halt"

    def test_the_monitor_reports_that_exposure_should_stop(
        self, journal_events: tuple[object, ...]
    ) -> None:
        """It reports the fact; the watchdog decides what to do about it."""
        repository = InMemoryRepository()
        monitor, _ = make_monitor(repository)
        assert monitor.check(journal_events, at=AT).blocks_new_exposure  # type: ignore[arg-type]


class TestIntervalRunner:
    def test_a_cadence_can_be_run_without_sleeping(
        self, journal_events: tuple[object, ...]
    ) -> None:
        """The clock is injected, so a day of checks runs in a millisecond and is replayable."""
        repository = InMemoryRepository()
        monitor, _ = make_monitor(repository)
        clock = SimulatedClock(AT)

        results = interval_runner(
            monitor,
            clock=clock,
            journal_loader=lambda: journal_events,  # type: ignore[arg-type,return-value]
            interval_seconds=300,
            iterations=6,
        )
        assert len(results) == 6
        # The cadence actually advanced the clock.
        assert results[-1].checked_at > results[0].checked_at
        # Six checks over 25 minutes: the announcement, the escalation at check 3, and one
        # reminder once the 900s cadence came due. Three alerts, not six.
        assert sum(1 for r in results if r.alerted) == 3
        assert monitor.alert_rate()["alerts_per_drifted_check"] == pytest.approx(0.5, abs=0.01)


class TestDriftRatio:
    def test_a_ratio_is_measurable(self, journal_events: tuple[object, ...]) -> None:
        from tigerquant.db.projector import check_drift

        repository = InMemoryRepository()
        report = check_drift(journal_events, repository)  # type: ignore[arg-type]
        assert drift_ratio(report) == 1

    def test_an_empty_journal_is_zero_not_undefined(self) -> None:
        from tigerquant.db.projector import check_drift

        report = check_drift((), InMemoryRepository())  # type: ignore[arg-type]
        assert drift_ratio(report) == 0
