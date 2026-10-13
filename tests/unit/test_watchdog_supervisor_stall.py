"""CR-07: a hung supervisor must reach the watchdog, not just the console.

Closes the limitation named in the hang-detection evidence: *"A hang is not escalated. The verdict is
printed; no alert is raised, and nothing halts. CR-07's watchdog is the right owner and does not
consult this yet."*

**A hang nobody is alerted to is half a feature.** The watchdog's whole purpose is to stop the system
when something is wrong, and a frozen supervisor cannot reconcile -- which ADR-0008 says must complete
before a restart permits new exposure.

**THE WATCHDOG TAKES A VERDICT, NOT A PATH.** It is a deterministic evaluator that "reads metrics and
conditions, never prose", and reading a file is a side effect. The caller detects and passes the
verdict. That also lets these tests run with no heartbeat on disk, which matters because the sandbox
here cannot create temp directories.

**`None` MEANS NO CHECK WAS PERFORMED**, which is deliberately different from a verdict of `STALE`.
Not looking is not the same as looking and finding nothing, and collapsing them would let an unchecked
system report as checked.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tigerquant.core.enums import ReasonCode, Severity
from tigerquant.ops.supervisor import HangVerdict
from tigerquant.ops.watchdog import Watchdog
from tigerquant.risk import RiskPolicy, RiskState


def policy() -> RiskPolicy:
    return RiskPolicy(
        policy_version="cr07-test",
        universe_version="u",
        allowed_instruments=("SPY",),
        gross_exposure_fraction=Decimal("0.5"),
        max_symbol_fraction=Decimal("0.1"),
        max_order_fraction=Decimal("0.1"),
        max_mark_age_seconds=900,
        max_orders_per_cycle=1,
        max_turnover_fraction=Decimal("2.0"),
        max_daily_loss_fraction=Decimal("0.02"),
        max_drawdown_fraction=Decimal("0.05"),
        max_orders_per_hour=30,
    )


def watchdog() -> Watchdog:
    return Watchdog(policy=policy(), state=RiskState())


def stalled(verdict: HangVerdict | None) -> list:
    return [
        item
        for item in watchdog().inspect(metrics=None, supervisor_verdict=verdict)
        if item.code is ReasonCode.SUPERVISOR_STALLED
    ]


class TestAHangHalts:
    def test_a_hung_supervisor_produces_a_verdict(self) -> None:
        """THE case. Before this, a hang was printed and nothing acted on it."""
        assert stalled(HangVerdict.HUNG), "a hung supervisor raised no watchdog verdict"

    def test_it_is_critical(self) -> None:
        """CRITICAL is what halts. A hang means reconciliation cannot complete, and ADR-0008 blocks
        new exposure until it does."""
        assert stalled(HangVerdict.HUNG)[0].severity is Severity.CRITICAL

    def test_the_reason_code_names_a_stall_not_a_crash(self) -> None:
        """A hung process may still hold a broker connection, so treating it as absent would be
        wrong about what a responder is dealing with."""
        assert stalled(HangVerdict.HUNG)[0].code is ReasonCode.SUPERVISOR_STALLED

    def test_the_detail_says_why_it_matters(self) -> None:
        detail = stalled(HangVerdict.HUNG)[0].detail
        assert "reconcile" in detail
        assert "STALL" in detail


class TestUncertaintyWarnsRatherThanHalts:
    """Halting on an absent heartbeat would stop a system that has done nothing wrong."""

    @pytest.mark.parametrize("verdict", [HangVerdict.STALE, HangVerdict.UNKNOWN])
    def test_it_warns(self, verdict: HangVerdict) -> None:
        found = stalled(verdict)
        assert found, f"{verdict.name} produced no verdict at all"
        assert found[0].severity is Severity.WARNING

    @pytest.mark.parametrize("verdict", [HangVerdict.STALE, HangVerdict.UNKNOWN])
    def test_it_does_not_halt(self, verdict: HangVerdict) -> None:
        assert all(item.severity is not Severity.CRITICAL for item in stalled(verdict))

    def test_a_first_run_is_not_a_fault(self) -> None:
        """An absent heartbeat is a first run as easily as a fault, which is why STALE warns."""
        assert stalled(HangVerdict.STALE)[0].severity is Severity.WARNING


class TestHealthyVerdictsAreSilent:
    @pytest.mark.parametrize(
        "verdict",
        [HangVerdict.IDLE, HangVerdict.BACKOFF, HangVerdict.WORKING],
    )
    def test_nothing_is_raised(self, verdict: HangVerdict) -> None:
        """A watchdog that flags normal operation is a watchdog that gets ignored."""
        assert stalled(verdict) == []

    def test_a_long_backoff_raises_nothing(self) -> None:
        """The case the detector itself was built to get right, asserted through the WATCHDOG as
        well -- the escalation is what an operator would actually be stopped by."""
        assert stalled(HangVerdict.BACKOFF) == []


class TestNotLookingIsNotTheSameAsLooking:
    def test_no_verdict_produces_nothing(self) -> None:
        """`None` means the check was not performed. Treating that as STALE would make an unchecked
        system indistinguishable from a checked one."""
        assert stalled(None) == []

    def test_the_parameter_defaults_to_none(self) -> None:
        """Existing callers pass no verdict, so the default must not change their behaviour."""
        items = watchdog().inspect(metrics=None)
        assert [item for item in items if item.code is ReasonCode.SUPERVISOR_STALLED] == []

    def test_a_caller_that_does_not_pass_it_is_unaffected(self) -> None:
        with_verdict = watchdog().inspect(metrics=None, supervisor_verdict=HangVerdict.IDLE)
        without = watchdog().inspect(metrics=None)
        assert len(with_verdict) == len(without)


class TestItComposesWithTheOtherConditions:
    def test_a_hang_does_not_suppress_another_verdict(self) -> None:
        """Conditions are additive. A stalled supervisor AND an unreconciled account are both worth
        reporting, and reporting one must not hide the other."""
        items = watchdog().inspect(
            metrics=None, account_reconciled=False, supervisor_verdict=HangVerdict.HUNG
        )
        codes = {item.code for item in items}
        assert ReasonCode.SUPERVISOR_STALLED in codes
        assert ReasonCode.UNRECONCILED in codes

    def test_engage_still_works_with_a_hang_present(self) -> None:
        """The verdict must be usable by the halting path, not merely produced."""
        import datetime as dt

        w = watchdog()
        verdicts = w.inspect(metrics=None, supervisor_verdict=HangVerdict.HUNG)
        assert w.engage(verdicts, at=dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC))
