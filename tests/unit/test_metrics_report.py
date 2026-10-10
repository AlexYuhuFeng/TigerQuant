"""CR-09: measured session metrics — P&L attribution, step durations, activity counts.

The most important assertions here are about **labelling**. The offline replay drives a
``SimulatedClock``, so every duration it produces is a fixture step size. Reporting those as
latency would be the single most misleading thing this module could do, so the tests pin that
they are labelled as step durations and that ``is_observed`` is False.

The second theme is arithmetic honesty: a P&L attribution whose parts do not add up to the
ledger's movement must say so and name the residual, rather than publishing a tidy number that
is quietly wrong.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tigerquant.core.enums import Mode
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.market.fixtures import load_scenario
from tigerquant.ops.metrics_report import (
    AttributionReport,
    LatencyReport,
    StepDuration,
    attribution_by_decision,
    measure_session,
)
from tigerquant.pipeline.replay import run_replay

FIXTURE = "tests/fixtures/scenarios/trend_up.yaml"


@pytest.fixture
def events(tmp_path: pytest.TempPathFactory) -> tuple[object, ...]:
    path = tmp_path / "journal.jsonl"  # type: ignore[operator]
    run_replay(load_scenario(FIXTURE), journal_path=path)
    with AppendOnlyJournal(path, Mode.BACKTEST, fsync=False) as journal:
        return tuple(record.event for record in journal.iter_records())


@pytest.fixture
def session(events: tuple[object, ...]) -> object:
    return measure_session(events, mode=Mode.BACKTEST.value)


class TestHonestLabelling:
    def test_simulated_durations_are_not_called_latency(self, session: object) -> None:
        """The whole reason this module has a clock_source field."""
        assert not session.latency.is_observed  # type: ignore[attr-defined]
        assert session.latency.label == "step duration (simulated clock)"  # type: ignore[attr-defined]
        assert "NOT a latency measurement" in session.latency.summary()  # type: ignore[attr-defined]

    def test_the_clock_source_is_carried_into_the_record(self, session: object) -> None:
        record = session.as_record()  # type: ignore[attr-defined]
        assert record["latency"]["clock_source"] == "SimulatedClock"
        assert record["latency"]["is_observed_latency"] is False

    def test_the_facts_state_it_in_words(self, session: object) -> None:
        assert session.facts["durations_are_observed"] == "False"  # type: ignore[attr-defined]
        assert "SIMULATED clock" in session.facts["note"]  # type: ignore[attr-defined]

    def test_a_real_clock_would_be_labelled_differently(self, events: tuple[object, ...]) -> None:
        """The label follows the injected clock, not a constant."""
        real = measure_session(events, mode="PAPER", clock_source="SystemClock")
        assert real.latency.is_observed
        assert real.latency.label == "latency"
        assert "NOT a latency measurement" not in real.latency.summary()
        assert real.facts["durations_are_observed"] == "True"

    def test_the_default_is_the_conservative_label(self, events: tuple[object, ...]) -> None:
        """Defaulting to a real clock would let an offline replay claim to measure latency."""
        default = measure_session(events, mode="BACKTEST")
        assert not default.latency.is_observed


class TestActivityCounts:
    def test_counts_match_the_journal(self, session: object, events: tuple[object, ...]) -> None:
        assert session.events == len(events)  # type: ignore[attr-defined]
        assert session.decisions == 6  # type: ignore[attr-defined]
        assert session.orders == 1  # type: ignore[attr-defined]
        assert session.fills == 1  # type: ignore[attr-defined]
        assert session.mark_count == 6  # type: ignore[attr-defined]

    def test_the_session_duration_is_measured(self, session: object) -> None:
        assert session.duration_seconds == 360  # type: ignore[attr-defined]

    def test_an_empty_journal_measures_zero_rather_than_failing(self) -> None:
        empty = measure_session((), mode="BACKTEST")  # type: ignore[arg-type]
        assert empty.events == 0
        assert empty.decisions == 0
        assert empty.attribution.closing_equity == Decimal(0)
        assert not empty.latency.is_observed


class TestAttributionArithmetic:
    def test_the_equity_change_is_read_from_the_ledger(self, session: object) -> None:
        attribution = session.attribution  # type: ignore[attr-defined]
        assert attribution.opening_equity == Decimal("100000.00")
        assert attribution.closing_equity == Decimal("100015.48")
        assert attribution.equity_change == Decimal("15.48")

    def test_commission_is_reported_separately_from_trading_result(self, session: object) -> None:
        """Folding fees into P&L hides how much of a result friction ate."""
        attribution = session.attribution  # type: ignore[attr-defined]
        assert attribution.commission_paid == Decimal("0.04")
        assert attribution.net_pnl == attribution.gross_pnl - attribution.commission_paid

    def test_a_residual_is_named_rather_than_hidden(self, session: object) -> None:
        """This journal genuinely does not reconcile, and the report says so.

        ``PositionChanged`` carries the mark that applied when the position changed, while
        ``LedgerMarked`` keeps being emitted as the mark moves, so the last position snapshot
        can be from an earlier instant than the final valuation. The difference is a real
        property of the event stream, not an arithmetic error, and the summary says which.
        """
        attribution = session.attribution  # type: ignore[attr-defined]
        assert not attribution.reconciles()
        assert attribution.residual != 0
        assert "RESIDUAL" in attribution.summary()
        assert "not an arithmetic error" in attribution.summary()

    def test_the_record_carries_the_residual(self, session: object) -> None:
        record = session.attribution.as_record()  # type: ignore[attr-defined]
        assert "residual" in record
        assert record["reconciles"] is False

    def test_a_hand_built_report_that_adds_up_reconciles(self) -> None:
        """The check is not simply 'always false'."""
        balanced = AttributionReport(
            opening_equity=Decimal("100.00"),
            closing_equity=Decimal("105.00"),
            closing_cash=Decimal("105.00"),
            realized_pnl=Decimal("5.00"),
            unrealized_pnl=Decimal(0),
            commission_paid=Decimal(0),
        )
        assert balanced.residual == 0
        assert balanced.reconciles()
        assert "reconciles" in balanced.summary()
        assert "RESIDUAL" not in balanced.summary()

    def test_a_cent_of_tolerance_is_allowed(self) -> None:
        """The ledger quantises cash to two places, so a cent of rounding is expected."""
        almost = AttributionReport(
            opening_equity=Decimal("100.00"),
            closing_equity=Decimal("105.00"),
            closing_cash=Decimal("105.00"),
            realized_pnl=Decimal("5.01"),
            unrealized_pnl=Decimal(0),
            commission_paid=Decimal(0),
        )
        assert almost.reconciles()

    def test_an_aggregate_only_report_does_not_invent_a_breakdown(self) -> None:
        """The first version summed the instrument breakdown, so a report with aggregates but
        no per-instrument rows returned -commission -- confidently wrong."""
        from tigerquant.ops.metrics_report import AttributionReport as R

        aggregate_only = R(
            opening_equity=Decimal("100.00"),
            closing_equity=Decimal("110.00"),
            closing_cash=Decimal("50.00"),
            realized_pnl=Decimal("2.00"),
            unrealized_pnl=Decimal("9.00"),
            commission_paid=Decimal("1.00"),
        )
        assert aggregate_only.instruments == ()
        assert aggregate_only.sum_of_parts == Decimal("10.00")
        assert aggregate_only.breakdown_agrees

    def test_components_sum_to_the_parts(self) -> None:
        report = AttributionReport(
            opening_equity=Decimal("100.00"),
            closing_equity=Decimal("110.00"),
            closing_cash=Decimal("50.00"),
            realized_pnl=Decimal("2.00"),
            unrealized_pnl=Decimal("9.00"),
            commission_paid=Decimal("1.00"),
        )
        assert report.gross_pnl == Decimal("11.00")
        assert report.net_pnl == Decimal("10.00")
        assert report.sum_of_parts == Decimal("10.00")
        assert report.equity_change == Decimal("10.00")
        assert report.reconciles()


class TestStepDurations:
    def test_hops_are_measured(self, session: object) -> None:
        hops = {step.hop for step in session.latency.steps}  # type: ignore[attr-defined]
        assert "decision->intent" in hops
        assert "ack->fill" in hops

    def test_percentiles_are_ordered(self, session: object) -> None:
        for step in session.latency.steps:  # type: ignore[attr-defined]
            assert step.minimum_ms <= step.median_ms <= step.p95_ms <= step.maximum_ms

    def test_a_hop_with_no_samples_is_omitted(self) -> None:
        report = LatencyReport(clock_source="SimulatedClock", steps=())
        assert report.steps == ()
        assert "nothing measured" in report.summary()

    def test_a_step_record_is_flat(self) -> None:
        step = StepDuration(hop="a->b", count=3, minimum_ms=1, median_ms=2, p95_ms=4, maximum_ms=9)
        record = step.as_record()
        assert record["hop"] == "a->b"
        assert set(record) == {"hop", "count", "min_ms", "median_ms", "p95_ms", "max_ms"}


class TestByDecision:
    def test_each_decision_gets_a_row(self, events: tuple[object, ...]) -> None:
        rows = attribution_by_decision(events, mode=Mode.BACKTEST.value)  # type: ignore[arg-type]
        assert len(rows) == 6
        assert all("decision_id" in row for row in rows)

    def test_only_one_decision_reached_the_venue(self, events: tuple[object, ...]) -> None:
        rows = attribution_by_decision(events, mode=Mode.BACKTEST.value)  # type: ignore[arg-type]
        assert sum(1 for row in rows if row["reached_the_venue"]) == 1

    def test_the_traded_decision_carries_its_notional(self, events: tuple[object, ...]) -> None:
        rows = attribution_by_decision(events, mode=Mode.BACKTEST.value)  # type: ignore[arg-type]
        traded = next(row for row in rows if row["reached_the_venue"])
        assert traded["filled_quantity"] == 8
        assert Decimal(str(traded["traded_notional"])) > 0


class TestSessionRecord:
    def test_the_record_is_serialisable_and_carries_a_digest(self, session: object) -> None:
        import json

        record = session.as_record()  # type: ignore[attr-defined]
        assert json.dumps(record)
        assert record["digest"] == session.digest()  # type: ignore[attr-defined]

    def test_the_digest_is_stable(self, events: tuple[object, ...]) -> None:
        first = measure_session(events, mode="BACKTEST")  # type: ignore[arg-type]
        second = measure_session(events, mode="BACKTEST")  # type: ignore[arg-type]
        assert first.digest() == second.digest()

    def test_the_summary_reads_like_a_report(self, session: object) -> None:
        summary = session.summary()  # type: ignore[attr-defined]
        assert "session [BACKTEST]" in summary
        assert "P&L" in summary
