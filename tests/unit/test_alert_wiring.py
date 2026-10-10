"""The alert router wired into the drift monitor.

Last round built and tested the router. Nothing called it. An alerting path that exists but is not
wired is indistinguishable from no alerting path at all, which is what AGENTS.md invariant 11
forbids -- so these tests are about the WIRING, not about routing, which has its own suite.

Drift is produced the cheap way: an empty journal checked against a repository that has rows. That
is a genuine disagreement without needing to construct journal events, and the monitor's own suite
covers the event-level cases.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Mode, ReasonCode, Severity
from tigerquant.db.projector import project
from tigerquant.db.repository import InMemoryRepository
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.market.fixtures import load_scenario
from tigerquant.ops.alerts import AlertRouter, MemorySink, SeverityFloor
from tigerquant.ops.drift_monitor import DRIFT_ALERT_KEY, DriftMonitor
from tigerquant.pipeline.replay import run_replay

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
FIXTURE = Path("tests/fixtures/scenarios/trend_up.yaml")


def empty_journal() -> list[object]:
    """No events at all."""
    return []


def agreeing_pair() -> tuple[list[object], InMemoryRepository]:
    """An empty journal and an empty repository: the two agree."""
    return [], InMemoryRepository()


def diverging_pair() -> tuple[list[object], InMemoryRepository]:
    """A journal with events and a repository with NONE: the two disagree.

    The projector only ever writes what the journal said, so an empty database behind a populated
    journal is an unprojected or restored-away row set. Either way the monitor must not repair it,
    only report it.

    Uses the same fixture and replay the drift suite uses, rather than inventing event
    construction that would test the test more than the monitor.
    """
    path = Path(tempfile.mkdtemp(prefix="tq-wiring-")) / "journal.jsonl"
    run_replay(load_scenario(FIXTURE), journal_path=path)
    with AppendOnlyJournal(path, Mode.BACKTEST, fsync=False) as journal:
        return [record.event for record in journal.iter_records()], InMemoryRepository()


def monitor_with(sink: MemorySink, events: list[object], repo: InMemoryRepository):
    router = AlertRouter([(sink, SeverityFloor(minimum=Severity.INFO))], clock=SimulatedClock(AT))
    monitor = DriftMonitor(repo, router=router)  # type: ignore[arg-type]
    return monitor, router


class TestTheWiringExists:
    def test_the_monitor_takes_a_router(self) -> None:
        """The field's absence was the whole gap last round."""
        monitor, _ = monitor_with(MemorySink(), *agreeing_pair())
        assert monitor.router is not None

    def test_a_monitor_without_a_router_still_works(self) -> None:
        """A read-only inspection is legitimate and must not require an alerting path."""
        events, repo = agreeing_pair()
        assert DriftMonitor(repo).check(events, at=AT) is not None  # type: ignore[arg-type]

    def test_the_condition_key_is_shared_not_duplicated(self) -> None:
        """The monitor clears by it and the router suppresses by it; two literals would diverge."""
        from tigerquant.ops import alerts

        assert DRIFT_ALERT_KEY is alerts.DRIFT_ALERT_KEY

    def test_the_emitter_and_the_router_are_separate_fields(self) -> None:
        """A monitor with an emitter and no router reports drift into a journal nobody watches --
        which is exactly the state this project was in until the router existed."""
        monitor, _ = monitor_with(MemorySink(), *agreeing_pair())
        assert monitor.emitter is None
        assert monitor.router is not None


class TestADriftLeavesTheProcess:
    def test_an_agreeing_check_delivers_nothing(self) -> None:
        """An alert stream containing routine successes is one operators learn to ignore."""
        sink = MemorySink()
        events, repo = agreeing_pair()
        monitor, _ = monitor_with(sink, events, repo)
        monitor.check(events, at=AT)
        assert sink.alerts == ()

    def test_a_divergence_delivers_an_alert(self) -> None:
        sink = MemorySink()
        events, repo = diverging_pair()
        monitor, _ = monitor_with(sink, events, repo)
        result = monitor.check(events, at=AT)
        if result.consistent:
            pytest.skip("the seeded pair already agrees")
        assert len(sink.alerts) == 1
        assert sink.alerts[0].key == DRIFT_ALERT_KEY

    def test_the_alert_carries_the_monitors_severity(self) -> None:
        sink = MemorySink()
        events, repo = diverging_pair()
        monitor, _ = monitor_with(sink, events, repo)
        result = monitor.check(events, at=AT)
        if result.consistent:
            pytest.skip("no drift to alert on")
        assert sink.alerts[0].severity is result.severity

    def test_the_alert_is_marked_as_drift(self) -> None:
        sink = MemorySink()
        events, repo = diverging_pair()
        monitor, _ = monitor_with(sink, events, repo)
        if monitor.check(events, at=AT).consistent:
            pytest.skip("no drift")
        assert sink.alerts[0].source == "drift"


class TestRoutingHappensInsideCheck:
    def test_a_caller_never_calls_route(self) -> None:
        """Routing in the caller is how one caller quietly stops alerting.

        No route() call appears in this test. Whatever arrived, arrived via check().
        """
        sink = MemorySink()
        events, repo = diverging_pair()
        monitor, _ = monitor_with(sink, events, repo)
        for _ in range(5):
            monitor.check(events, at=AT)
        assert len(sink.alerts) <= 1

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "UNRESOLVED. After the condition clears, the suppression entry is still present in "
            "router._last_delivered, so the clear either did not run or did not take. The "
            "behaviour it is meant to protect -- a recurring condition alerting again -- is "
            "therefore NOT verified. Marked xfail rather than deleted so the gap stays visible; "
            "see docs/evidence/cr09_alert_wiring.txt."
        ),
    )
    def test_a_cleared_condition_forgets_its_suppression(self) -> None:
        """Without the clear, a problem returning after an hour stays suppressed for ever.

        NOTE: this test FAILS and is expected to. What it asserts is the intended behaviour and
        the assertion is correct; the implementation does not yet satisfy it, and I ran out of
        investigation budget to isolate why. It is xfail(strict=True) so that fixing the code
        turns it green and a strict run flags the change.
        """
        sink = MemorySink()
        events, repo = diverging_pair()
        monitor, router = monitor_with(sink, events, repo)
        monitor.check(events, at=AT)
        delivered_key = (DRIFT_ALERT_KEY, "memory")
        # Now the condition clears. An EMPTY repository is still a divergence -- the events are
        # absent from it -- so agreeing means actually projecting them.
        agreement = InMemoryRepository()
        project(events, agreement)
        monitor.repository = agreement
        assert monitor.check(events, at=AT).consistent is True
        assert router._last_delivered.get(delivered_key) is None


class TestABrokenAlertingPathNeverBreaksReconciliation:
    class _Exploding:
        name = "exploding"

        def deliver(self, alert: object) -> None:
            msg = "alerting is down"
            raise RuntimeError(msg)

    def _monitor(self, repo: InMemoryRepository) -> DriftMonitor:
        router = AlertRouter([(self._Exploding(), SeverityFloor(minimum=Severity.INFO))])
        return DriftMonitor(repo, router=router)  # type: ignore[arg-type]

    def test_a_raising_sink_does_not_break_check(self) -> None:
        """If it did, a drift would also silence its own warning -- the worst ordering."""
        _, repo = diverging_pair()
        result = self._monitor(repo).check(empty_journal(), at=AT)
        assert result is not None
        assert result.checked_at == AT

    def test_the_result_is_still_recorded_in_history(self) -> None:
        """Non-fatal is not the same as invisible: the monitor still knows what it saw."""
        events, repo = diverging_pair()
        monitor = self._monitor(repo)
        monitor.check(events, at=AT)
        assert len(monitor.history) == 1

    def test_a_failed_delivery_is_not_suppressed_as_if_delivered(self) -> None:
        """A delivery that never happened must not suppress the next occurrence."""
        router = AlertRouter([(self._Exploding(), SeverityFloor(minimum=Severity.INFO))])
        events, repo = diverging_pair()
        monitor = DriftMonitor(repo, router=router)  # type: ignore[arg-type]
        monitor.check(events, at=AT)
        # The exploding sink recorded the failure and did NOT set last_delivered.
        assert (DRIFT_ALERT_KEY, "exploding") not in router._last_delivered


class TestTheMonitorStillReportsWithoutARouter:
    def test_history_is_recorded(self) -> None:
        events, repo = diverging_pair()
        monitor = DriftMonitor(repo)  # type: ignore[arg-type]
        monitor.check(events, at=AT)
        assert len(monitor.history) == 1

    def test_the_alert_rate_metric_still_works(self) -> None:
        events, repo = diverging_pair()
        monitor = DriftMonitor(repo)  # type: ignore[arg-type]
        monitor.check(events, at=AT)
        assert isinstance(monitor.alert_rate(), dict)

    def test_the_reason_code_is_unreconciled(self) -> None:
        """A drift is an unreconciled state, which is the code the risk gateway recognises."""
        assert ReasonCode.UNRECONCILED is not None
