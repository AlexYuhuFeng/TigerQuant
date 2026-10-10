"""The recurrence case the cleared-condition fix exists for.

The previous test asserts on `router._last_delivered`, which is an internal. This one asserts the
BEHAVIOUR an operator experiences: a drift that resolves and comes back must page again.

Why it matters concretely. Before the fix, `check()`'s consistent branch returned early without
calling `_route`, so `clear()` never ran. A drift would be delivered once and then suppressed for
ever -- including after it recovered and returned hours later. That is the failure mode of every
naive deduplicator, and it is the one an operator is least likely to notice, because the first
alert DID arrive and the system looks like it is working.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Mode, Severity
from tigerquant.db.projector import project
from tigerquant.db.repository import InMemoryRepository
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.market.fixtures import load_scenario
from tigerquant.ops.alerts import AlertRouter, MemorySink, SeverityFloor
from tigerquant.ops.drift_monitor import DRIFT_ALERT_KEY, DriftMonitor
from tigerquant.pipeline.replay import run_replay

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
FIXTURE = Path("tests/fixtures/scenarios/trend_up.yaml")


def journal_events() -> list[object]:
    path = Path(tempfile.mkdtemp(prefix="tq-recur-")) / "journal.jsonl"
    run_replay(load_scenario(FIXTURE), journal_path=path)
    with AppendOnlyJournal(path, Mode.BACKTEST, fsync=False) as journal:
        return [record.event for record in journal.iter_records()]


def router_with(sink: MemorySink) -> AlertRouter:
    return AlertRouter([(sink, SeverityFloor(minimum=Severity.INFO))], clock=SimulatedClock(AT))


class TestAConditionThatComesBackPagesAgain:
    def test_drift_clears_then_returns_is_delivered_twice(self) -> None:
        """The whole point of `clear()`, asserted as behaviour rather than as internal state."""
        events = journal_events()
        sink = MemorySink()
        router = router_with(sink)

        # A repository that agrees, so the monitor starts clean.
        agreement = InMemoryRepository()
        project(events, agreement)

        drifted = InMemoryRepository()  # empty: 31 events absent
        monitor = DriftMonitor(drifted, router=router)  # type: ignore[arg-type]

        # 1. The drift appears.
        assert monitor.check(events, at=AT).consistent is False
        assert len(sink.alerts) == 1
        assert sink.alerts[0].key == DRIFT_ALERT_KEY

        # 2. It resolves.
        monitor.repository = agreement
        assert monitor.check(events, at=AT).consistent is True

        # 3. It comes BACK. Before the fix this was silently suppressed for ever.
        monitor.repository = InMemoryRepository()
        assert monitor.check(events, at=AT).consistent is False
        assert len(sink.alerts) == 2, (
            "a recurring drift must page again; one alert for a condition that returned hours "
            "later is the failure this test exists for"
        )

    def test_repeats_within_one_outage_are_still_suppressed(self) -> None:
        """The fix must not have turned suppression off altogether."""
        events = journal_events()
        sink = MemorySink()
        monitor = DriftMonitor(InMemoryRepository(), router=router_with(sink))  # type: ignore[arg-type]

        for _ in range(6):
            monitor.check(events, at=AT)
        assert len(sink.alerts) == 1, "an unresolved condition must not page every cycle"

    def test_a_second_outage_pages_once_not_once_per_cycle(self) -> None:
        """Both halves at once: recurrence delivered, and only on the transition."""
        events = journal_events()
        sink = MemorySink()
        router = router_with(sink)
        agreement = InMemoryRepository()
        project(events, agreement)

        monitor = DriftMonitor(InMemoryRepository(), router=router)  # type: ignore[arg-type]
        monitor.check(events, at=AT)  # outage 1 -> delivered
        monitor.repository = agreement
        monitor.check(events, at=AT)  # recovered
        monitor.repository = InMemoryRepository()
        for _ in range(4):
            monitor.check(events, at=AT)  # outage 2 -> delivered once, then suppressed

        assert len(sink.alerts) == 2

    def test_the_recovery_is_recorded_even_though_no_alert_is_sent(self) -> None:
        """A recovery is INFO-level bookkeeping, not a page, but it must still be in history."""
        events = journal_events()
        sink = MemorySink()
        agreement = InMemoryRepository()
        project(events, agreement)
        monitor = DriftMonitor(InMemoryRepository(), router=router_with(sink))  # type: ignore[arg-type]

        monitor.check(events, at=AT)
        monitor.repository = agreement
        recovered = monitor.check(events, at=AT)

        assert recovered.consistent is True
        assert recovered.recovered is True
        assert len(monitor.history) == 2


class TestTheEarlyReturnIsGone:
    def test_the_consistent_branch_routes(self) -> None:
        """The defect was a `return` that skipped `_route`. Asserted structurally, so a future
        refactor that reintroduces an early return fails here rather than in the field."""
        import inspect

        source = inspect.getsource(DriftMonitor.check)
        consistent_branch = source[: source.index("self._state.consecutive_drifted += 1")]
        assert "_route(result)" in consistent_branch, (
            "the consistent branch of check() must route, or clear() never runs and a recurring "
            "condition stays suppressed for ever"
        )

    def test_both_return_paths_route(self) -> None:
        import inspect

        source = inspect.getsource(DriftMonitor.check)
        assert source.count("self._route(result)") == 2

    def test_the_drift_counter_still_increments(self) -> None:
        """The fix touched the line beside `consecutive_drifted += 1`; assert it survived.

        The first version of this fix DELETED that line, because the edit's match included it and
        the replacement did not. The suite caught it, which is why the suite runs on every change.
        """
        events = journal_events()
        monitor = DriftMonitor(InMemoryRepository())  # type: ignore[arg-type]
        for expected in (1, 2, 3):
            monitor.check(events, at=AT)
            assert monitor._state.consecutive_drifted == expected
