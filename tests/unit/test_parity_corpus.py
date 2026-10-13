"""CR-04: strategy-loop parity between the direct path and the Nautilus adapter.



Both paths end at the **same** broker, so any structural difference is a defect in the adapter
rather than a venue characteristic. That is what makes this a parity test instead of a
comparison of two unrelated things.

Running it found two real adapter bugs that reading the code did not:

1. the adapter stringified the engine's typed ``client_order_id`` and passed the string back
   into the engine's typed callbacks, which raise TypeError;
2. it passed ``venue_order_id=None`` to ``generate_order_accepted``, which requires a real
   ``VenueOrderId``.

Both only appear once a real engine callback is reached, which is exactly the coverage the
direct-path tests could not provide.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tigerquant.adapters.nautilus.parity_corpus import (
    CORPUS,
    ScenarioCase,
    build_approvals,
    run_corpus,
    run_scenario,
)
from tigerquant.core.enums import Mode, SubmissionTarget
from tigerquant.risk.state import RiskState

# This module reaches an optional SDK through the adapter it imports, so it needs that
# extra installed. The fast lane syncs only `--extra dev` and DESELECTS it rather than
# failing: the marker existed in pyproject all along and nothing wore it, so CI ran these
# against an environment that deliberately lacks the dependency. Placed after the imports
# so it neither lands inside the docstring nor trips E402.
pytestmark = pytest.mark.requires_engine

requires_engine = pytest.mark.requires_engine


class TestTheCorpus:
    def test_it_is_not_empty_and_every_case_is_named(self) -> None:
        assert len(CORPUS) >= 5
        names = [case.name for case in CORPUS]
        assert len(set(names)) == len(names), "case names must be unique"

    def test_every_case_has_at_least_one_order(self) -> None:
        assert all(case.orders for case in CORPUS)

    def test_every_order_has_a_distinct_client_id(self) -> None:
        """The adapter correlates by client order id, so a collision would be a harness bug."""
        for case in CORPUS:
            ids = [order.client_order_id for order in case.orders]
            assert len(set(ids)) == len(ids), case.name

    def test_approvals_cover_every_order(self) -> None:
        """The adapter refuses to submit without one, so the approvals are part of the case."""
        for case in CORPUS:
            approvals = build_approvals(case)
            assert set(approvals) == {order.client_order_id for order in case.orders}


@requires_engine
class TestParity:
    def test_the_whole_corpus_is_structurally_identical(self) -> None:
        report = run_corpus()
        assert report.is_parity, f"parity failure: {report.failures}\n{report.as_record()}"
        assert report.comparable == report.total

    def test_each_scenario_individually(self) -> None:
        for case in CORPUS:
            result = run_scenario(case)
            assert result.comparable, f"{case.name}: {[d.describe() for d in result.structural]}"

    def test_the_adapter_actually_submitted(self) -> None:
        """A parity test where neither path did anything would pass trivially."""
        case = CORPUS[0]
        report = run_scenario(case)
        assert report.left.steps, "the direct path recorded no steps"
        assert report.right.steps, "the adapter path recorded no steps"
        assert any(step.state for step in report.right.steps), "the adapter recorded no state"

    def test_the_adapter_path_reaches_the_broker(self) -> None:
        """Parity only means something if the adapter path really reached the same broker."""
        case = CORPUS[0]
        report = run_scenario(case)
        assert report.right.broker == "nautilus-adapter"
        assert any(step.broker_order_id for step in report.right.steps)


@requires_engine
class TestTheAdapterGatesStillHold:
    def test_a_none_target_still_refuses_through_the_corpus(self) -> None:
        """Parity must not have been achieved by weakening a gate."""
        from tigerquant.adapters.nautilus.execution_client import SubmissionGate
        from tigerquant.core.errors import RiskDenied

        gate = SubmissionGate(submission_target=SubmissionTarget.NONE, mode=Mode.SHADOW)
        with pytest.raises(RiskDenied):
            gate.check(client_order_id="TQ-whatever")

    def test_a_halt_still_blocks_new_exposure(self) -> None:
        state = RiskState()
        assert not state.blocks_new_exposure()


class TestTheMoveThatFixedThreeFailures:
    def test_the_harness_lives_in_the_adapter_package(self) -> None:
        """It imports the engine, so L2 may not hold it.

        The first version sat in ``execution/parity_corpus.py`` and three of this project's own
        boundary tests failed: L2 importing L3, and an optional SDK imported outside
        ``adapters/``. Both rules were right, and the fix was to move the file rather than
        relax them.
        """
        import tigerquant.adapters.nautilus.parity_corpus as module

        assert "adapters" in module.__name__

    def test_the_execution_package_no_longer_holds_it(self) -> None:
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tigerquant.execution.parity_corpus")


class TestScenarioCase:
    def test_commands_are_deterministic_and_ordered(self) -> None:
        case = CORPUS[1]
        first = case.commands()
        second = case.commands()
        assert [c.client_order_id for c in first] == [c.client_order_id for c in second]
        assert [c.at for c in first] == sorted(c.at for c in first)

    def test_a_case_can_be_built_independently(self) -> None:
        """The corpus is additive: a new case is a new object, never an edit to an old one."""
        from tigerquant.adapters.nautilus.parity_corpus import _broker_fixture, _order

        custom = ScenarioCase(
            name="harness-selftest",
            fixture=_broker_fixture(),
            orders=(_order(99),),
            # The fake broker refuses to invent a fill price, which is deliberate: a silent
            # zero would book a fake trade. A case must supply its marks.
            marks={"SPY": Decimal("600.00")},
        )
        assert run_scenario(custom).comparable
