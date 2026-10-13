"""A state nobody established is not evidence of nothing.

`empty_or_unfilled` reported `True` for `BrokerOrderState.UNKNOWN`, with the docstring *"True when a
broker state means no exposure resulted"*. **UNKNOWN means the outcome is UNKNOWN** -- ADR-0008 says a
timeout or transport error after a submission may have been ACCEPTED -- so treating it as no exposure is
the exact mistake AGENTS.md invariant 8 exists to prevent.

The helper was dead, which is why the sweep found it as dead code rather than as a bug. **A dead helper
whose semantics are wrong is a trap for whoever wires it next**, and this project has an inventory of
things that look ready and are not.

Fixed rather than deleted: deleting removes the trap and the place the distinction is written down.
"""

from __future__ import annotations

import pytest

from tigerquant.execution.broker import BrokerOrderState
from tigerquant.execution.parity import empty_or_unfilled


class TestUnknownIsNotNoExposure:
    def test_unknown_is_not_treated_as_no_exposure(self) -> None:
        """THE invariant. A submission whose outcome nobody established may have been accepted."""
        assert empty_or_unfilled(BrokerOrderState.UNKNOWN) is False

    def test_a_rejection_proves_nothing_happened(self) -> None:
        assert empty_or_unfilled(BrokerOrderState.REJECTED) is True

    def test_a_cancellation_proves_nothing_happened(self) -> None:
        assert empty_or_unfilled(BrokerOrderState.CANCELED) is True

    @pytest.mark.parametrize(
        "state",
        [
            s
            for s in BrokerOrderState
            if s not in {BrokerOrderState.REJECTED, BrokerOrderState.CANCELED}
        ],
        ids=lambda s: s.name,
    )
    def test_no_other_state_is_treated_as_no_exposure(self, state: BrokerOrderState) -> None:
        """Stated over EVERY other state rather than the two that matter, so a state added later is
        safe by default. **A predicate that defaults to 'nothing happened' is the wrong default for
        exposure**: the safe answer to an unrecognised state is that it must be reconciled."""
        assert empty_or_unfilled(state) is False, (
            f"{state.name} is reported as no exposure, but nothing establishes that"
        )

    def test_the_states_that_do_prove_it_are_exactly_two(self) -> None:
        """A sanity check on the parametrisation above: if a third state started returning True the
        other test would still pass while this one would not."""
        proving = [s for s in BrokerOrderState if empty_or_unfilled(s)]
        assert set(proving) == {BrokerOrderState.REJECTED, BrokerOrderState.CANCELED}
