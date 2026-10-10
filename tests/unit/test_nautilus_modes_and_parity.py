"""CR-04: mode translation and execution parity.

Two things are tested here, and neither needs the engine installed.

**Mode translation.** NautilusTrader has no ``PAPER`` environment, so our four modes map onto
its three with an explicit table. The tests pin the mapping and, more importantly, pin the two
refusals: ``LIVE`` has no translation, and ``PAPER`` will not default its submission target in
either direction.

**Parity.** Two broker implementations are driven through the identical command list and the
resulting traces are compared. The comparison must distinguish a *contract violation* (the two
paths implement different state machines — a failure) from a *venue characteristic* (a
different fill price — expected, and reported rather than smoothed away).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.adapters.nautilus.modes import (
    ENGINE_ENVIRONMENTS,
    describe_translation_table,
    translate,
)
from tigerquant.core.enums import Mode, SubmissionTarget
from tigerquant.core.errors import ConfigurationError
from tigerquant.execution.parity import (
    OrderCommand,
    OrderCommandKind,
    compare_traces,
    record_trace,
)

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


class TestModeTranslation:
    """ADR-0010: the mapping is explicit because the obvious mapping is wrong."""

    def test_the_engine_has_no_paper_environment(self) -> None:
        """If this ever changes, the whole translation table must be revisited."""
        assert ENGINE_ENVIRONMENTS == ("BACKTEST", "SANDBOX", "LIVE")
        assert "PAPER" not in ENGINE_ENVIRONMENTS

    def test_backtest_maps_to_backtest(self) -> None:
        result = translate(Mode.BACKTEST)
        assert result.engine_environment == "BACKTEST"
        assert result.submission_target is SubmissionTarget.NONE
        assert not result.engine_may_execute
        assert result.implementation_status == "SIMULATED"

    def test_paper_with_a_broker_maps_to_live_not_sandbox(self) -> None:
        """The single most important row in the table.

        A Tiger paper account is reached over the ordinary production gateway with a real API
        and a real order state machine, so the engine must be LIVE to connect. What makes it
        paper is the ACCOUNT IDENTITY, verified from the broker's own response — not the
        engine environment. Mapping PAPER to SANDBOX because both sound like "not real money"
        would label a real venue connection as a local simulation.
        """
        result = translate(Mode.PAPER, submission_target=SubmissionTarget.BROKER)
        assert result.engine_environment == "LIVE"
        assert result.engine_may_execute
        # But never LIVE_VERIFIED: the venue account is a simulator.
        assert result.implementation_status == "PAPER_VERIFIED"
        assert result.implementation_status != "LIVE_VERIFIED"

    def test_paper_without_a_target_is_refused(self) -> None:
        """Neither default is acceptable, so the caller must say which it means.

        Defaulting to BROKER would let an unconfigured run reach a real venue; defaulting to
        NONE would silently turn a paper run into a simulation.
        """
        with pytest.raises(ConfigurationError, match="explicit submission_target"):
            translate(Mode.PAPER)

    def test_paper_with_no_target_is_a_labelled_simulation(self) -> None:
        result = translate(Mode.PAPER, submission_target=SubmissionTarget.NONE)
        assert result.implementation_status == "SIMULATED"
        assert not result.engine_may_execute
        assert "no broker was involved" in result.rationale

    def test_shadow_runs_but_submits_nothing(self) -> None:
        result = translate(Mode.SHADOW)
        assert result.engine_environment == "SANDBOX"
        assert result.submission_target is SubmissionTarget.NONE
        assert not result.engine_may_execute

    def test_live_has_no_translation_and_that_is_the_control(self) -> None:
        """Not a missing implementation: translating would arm the one path that must stay off."""
        with pytest.raises(ConfigurationError) as caught:
            translate(Mode.LIVE, submission_target=SubmissionTarget.BROKER)
        message = str(caught.value)
        assert "never will in autonomous development" in message
        assert "human authorization" in message

    def test_backtest_cannot_be_given_a_target(self) -> None:
        with pytest.raises(ConfigurationError, match="not a backtest"):
            translate(Mode.BACKTEST, submission_target=SubmissionTarget.BROKER)

    def test_shadow_cannot_be_given_a_target(self) -> None:
        with pytest.raises(ConfigurationError, match="never submits"):
            translate(Mode.SHADOW, submission_target=SubmissionTarget.SIMULATION)

    def test_every_mode_has_a_row_and_live_is_refused_in_all_of_them(self) -> None:
        table = describe_translation_table()
        modes = {row["mode"] for row in table}
        assert modes == {mode.value for mode in Mode}
        live_rows = [row for row in table if row["mode"] == Mode.LIVE.value]
        assert live_rows
        assert all(row["engine_environment"] == "(refused)" for row in live_rows)
        # Every other mode has a concrete environment.
        assert all(
            row["engine_environment"] != "(refused)"
            for row in table
            if row["mode"] != Mode.LIVE.value
        )

    def test_no_translation_ever_reports_live_verified(self) -> None:
        """Nothing this project runs may claim live verification."""
        assert all(row["status"] != "LIVE_VERIFIED" for row in describe_translation_table())


# ---------------------------------------------------------------------------
# Parity: two brokers, one contract
# ---------------------------------------------------------------------------
class TestParityHarness:
    def test_a_broker_is_identical_to_itself(self) -> None:
        from tigerquant.execution.fake_broker import DeterministicFakeBroker
        from tigerquant.market.fixtures import BrokerFixture

        spec = BrokerFixture()
        commands: list[OrderCommand] = []

        left = record_trace(
            DeterministicFakeBroker(spec, mark_provider=lambda _: Decimal(600)), commands
        )
        right = record_trace(
            DeterministicFakeBroker(spec, mark_provider=lambda _: Decimal(600)), commands
        )
        report = compare_traces(left, right)
        assert report.comparable
        assert not report.divergences
        assert "identical" in report.summary()

    def test_an_empty_scenario_is_trivially_comparable(self) -> None:
        left = record_trace(_NoopBroker(), [])
        right = record_trace(_NoopBroker(), [])
        report = compare_traces(left, right)
        assert report.comparable
        assert report.left.digest() == report.right.digest()

    def test_a_different_step_count_is_structural(self) -> None:
        left = record_trace(_NoopBroker(), [])
        right = record_trace(_NoopBroker(), [OrderCommand(OrderCommandKind.QUERY, "x", AT)])
        report = compare_traces(left, right)
        assert not report.comparable
        assert any(d.field == "step_count" for d in report.structural)
        assert "STRUCTURAL MISMATCH" in report.summary()

    def test_a_different_state_is_structural(self) -> None:
        """Different state machines are a contract violation, not a venue characteristic."""
        left = record_trace(
            _ScriptedBroker("FILLED"), [OrderCommand(OrderCommandKind.QUERY, "a", AT)]
        )
        right = record_trace(
            _ScriptedBroker("REJECTED"), [OrderCommand(OrderCommandKind.QUERY, "a", AT)]
        )
        report = compare_traces(left, right)
        assert not report.comparable
        assert any(d.field == "state" for d in report.structural)
        assert "Comparable" not in report.summary()

    def test_a_different_fill_price_is_labelled_not_structural(self) -> None:
        """A simulator filling at the mark and a venue filling at a worse price are both
        correct. Reporting it as a failure would make parity testing useless; hiding it would
        make it dishonest."""
        left = record_trace(_ScriptedBroker("FILLED", price=Decimal("600.00")), [_query()])
        right = record_trace(_ScriptedBroker("FILLED", price=Decimal("600.25")), [_query()])
        report = compare_traces(left, right)
        assert report.comparable
        assert report.labelled
        assert not report.structural
        assert "does NOT mean interchangeable" in report.summary()

    def test_an_order_id_presence_difference_is_structural(self) -> None:
        left = record_trace(_ScriptedBroker("ACCEPTED", order_id="B-1"), [_query()])
        right = record_trace(_ScriptedBroker("ACCEPTED", order_id=None), [_query()])
        report = compare_traces(left, right)
        assert not report.comparable
        assert any(d.field == "broker_order_id" for d in report.structural)

    def test_the_report_is_serialisable_and_carries_both_digests(self) -> None:
        left = record_trace(_ScriptedBroker("FILLED"), [_query()])
        right = record_trace(_ScriptedBroker("FILLED"), [_query()])
        record = compare_traces(left, right).as_record()
        assert record["comparable"] is True
        assert record["left_digest"] == record["right_digest"]

    def test_a_structural_difference_changes_the_digest(self) -> None:
        left = record_trace(_ScriptedBroker("FILLED"), [_query()])
        right = record_trace(_ScriptedBroker("REJECTED"), [_query()])
        assert left.digest() != right.digest()

    def test_a_command_without_an_approval_is_skipped_in_both_traces(self) -> None:
        """The harness must not fail differently on the two sides for a harness reason."""
        commands = [OrderCommand(OrderCommandKind.SUBMIT, "missing-approval", AT)]
        left = record_trace(_NoopBroker(), commands)
        right = record_trace(_NoopBroker(), commands)
        report = compare_traces(left, right)
        assert report.comparable
        assert "skipped in both traces" in left.steps[0].message


def _query() -> OrderCommand:
    return OrderCommand(OrderCommandKind.QUERY, "c-1", AT)


class _NoopBroker:
    """A broker that reports nothing, for harness-level tests."""

    def submit(self, approval: object, *, at: dt.datetime) -> object:
        msg = "not used"
        raise AssertionError(msg)

    def cancel(self, *, client_order_id: str, at: dt.datetime) -> object:
        msg = "not used"
        raise AssertionError(msg)

    def query(self, *, client_order_id: str) -> object | None:
        return None

    def poll(self, *, at: dt.datetime) -> tuple[object, ...]:
        return ()


class _ScriptedBroker:
    """A broker whose single order answers exactly as told."""

    def __init__(
        self, state: str, *, price: Decimal | None = None, order_id: str | None = "B-1"
    ) -> None:
        self._state = state
        self._price = price
        self._order_id = order_id

    def query(self, *, client_order_id: str) -> object:
        from tigerquant.execution.broker import BrokerFill, BrokerOrderState, BrokerOrderView

        fills: tuple[BrokerFill, ...] = ()
        filled = 0
        if self._price is not None:
            filled = 8
            fills = (
                BrokerFill(
                    exec_id="e-1",
                    quantity=8,
                    price=self._price,
                    commission=Decimal(0),
                    occurred_at=AT,
                    broker_order_id=self._order_id or "",
                ),
            )
        return BrokerOrderView(
            client_order_id=client_order_id,
            broker_order_id=self._order_id,
            state=BrokerOrderState(self._state),
            filled_quantity=filled,
            remaining=8 - filled,
            fills=fills,
        )

    def submit(self, approval: object, *, at: dt.datetime) -> object:
        msg = "not used"
        raise AssertionError(msg)

    def cancel(self, *, client_order_id: str, at: dt.datetime) -> object:
        msg = "not used"
        raise AssertionError(msg)

    def poll(self, *, at: dt.datetime) -> tuple[object, ...]:
        return ()
