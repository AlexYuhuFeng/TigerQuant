"""CR-09: in-process hang detection -- alive but not progressing.

The supervisor's own docstring states the problem:

    "A process that is alive but stuck -- blocked on a database that stopped answering, or spinning
     ... Writing at the start too means the heartbeat carries the cycle it is IN, so a watchdog can
     see the index stop advancing -- which is the actual failure it needs to detect."

**Nothing implemented that distinction.** The heartbeat carried everything needed and no reader used
it. CR-09 listed in-process hang detection as remaining, and this closes it.

**THE SIGNAL.** The heartbeat is written at the START of a cycle (`running`) and again at the END
(`idle`/`backoff`). During a long cycle the phase stays `running` and `written_at` stops advancing.
That is precisely why writing twice was the right design -- and it is what makes the detector possible
without a second thread or a timeout wrapper.

**ONLY `running` CAN HANG.** A long `backoff` is the mechanism WORKING; long `idle` is a supervisor
between cycles. Reporting either as hung would fire on correct behaviour, and **a detector that cries
wolf gets turned off** -- the same reasoning this project applied to PARTIAL gate cases and to the
accepted CVE.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tigerquant.ops.supervisor import HangVerdict, detect_hang

NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)
BOUND = 300.0


def heartbeat(phase: str, *, age: float = 10.0, cycle: object = 7) -> dict[str, object]:
    return {
        "phase": phase,
        "cycle": cycle,
        "written_at": (NOW - dt.timedelta(seconds=age)).isoformat(),
    }


def verdict(beat: dict[str, object] | None, *, bound: float = BOUND) -> HangVerdict:
    return detect_hang(beat, now=NOW, hang_after_seconds=bound)[0]


class TestTheCaseThisExistsFor:
    def test_a_cycle_inside_the_bound_is_working(self) -> None:
        assert verdict(heartbeat("running", age=10)) is HangVerdict.WORKING

    def test_a_cycle_past_the_bound_is_hung(self) -> None:
        """THE case. The process is alive and has not progressed, which no liveness check sees."""
        assert verdict(heartbeat("running", age=400)) is HangVerdict.HUNG

    def test_the_boundary_is_inclusive_of_working(self) -> None:
        assert verdict(heartbeat("running", age=BOUND)) is HangVerdict.WORKING

    def test_just_past_the_boundary_is_hung(self) -> None:
        assert verdict(heartbeat("running", age=BOUND + 1)) is HangVerdict.HUNG

    def test_hung_is_not_healthy_but_working_is(self) -> None:
        assert not HangVerdict.HUNG.is_healthy
        assert HangVerdict.WORKING.is_healthy


class TestOnlyRunningCanHang:
    """The property that keeps the detector from crying wolf. Asserted for every other phase."""

    def test_a_long_backoff_is_not_a_hang(self) -> None:
        """Backoff EXISTS to wait. Long is the mechanism working, not a fault."""
        assert verdict(heartbeat("backoff", age=100_000)) is HangVerdict.BACKOFF

    def test_a_long_idle_is_not_a_hang(self) -> None:
        """A supervisor between cycles is idle. Its interval is not a fault."""
        assert verdict(heartbeat("idle", age=100_000)) is HangVerdict.IDLE

    @pytest.mark.parametrize("phase", ["backoff", "idle"])
    def test_those_phases_are_healthy_however_long(self, phase: str) -> None:
        assert verdict(heartbeat(phase, age=10**7)).is_healthy

    def test_only_running_is_hangable(self) -> None:
        hangable = [
            phase
            for phase in ("running", "idle", "backoff")
            if verdict(heartbeat(phase, age=10**6)) is HangVerdict.HUNG
        ]
        assert hangable == ["running"], f"these phases can be reported hung: {hangable}"


class TestStaleAndUnknownAreDifferentFromHung:
    def test_an_absent_heartbeat_is_stale_not_hung(self) -> None:
        """Different failures, different responses. A hung process holds resources; a gone one does
        not. Collapsing them would lose what a responder needs at 3am."""
        assert verdict(None) is HangVerdict.STALE

    def test_stale_is_not_healthy(self) -> None:
        assert not HangVerdict.STALE.is_healthy

    def test_a_heartbeat_without_written_at_is_unknown(self) -> None:
        assert verdict({"phase": "running", "cycle": 1}) is HangVerdict.UNKNOWN

    def test_an_unparseable_timestamp_is_unknown(self) -> None:
        assert verdict({"phase": "running", "written_at": "not-a-time"}) is HangVerdict.UNKNOWN

    def test_a_naive_timestamp_is_unknown(self) -> None:
        """A timestamp without a timezone cannot be compared to `now`, and guessing a zone would
        silently mis-measure the age."""
        beat = {"phase": "running", "written_at": "2026-10-13T11:00:00", "cycle": 1}
        assert verdict(beat) is HangVerdict.UNKNOWN

    def test_an_unrecognised_phase_is_unknown(self) -> None:
        """A phase a future version adds must not be silently treated as healthy."""
        assert verdict(heartbeat("something-new")) is HangVerdict.UNKNOWN

    def test_unknown_is_not_healthy(self) -> None:
        assert not HangVerdict.UNKNOWN.is_healthy


class TestTheReasonIsUsable:
    def test_a_hang_names_the_cycle_and_the_age(self) -> None:
        """A bare "HUNG" does not tell a responder whether to restart the process or look at what it
        was waiting on."""
        _, reason = detect_hang(
            heartbeat("running", age=400, cycle=41), now=NOW, hang_after_seconds=BOUND
        )
        assert "41" in reason
        assert "400" in reason

    def test_a_hang_says_it_is_not_progressing(self) -> None:
        _, reason = detect_hang(heartbeat("running", age=400), now=NOW, hang_after_seconds=BOUND)
        assert "not progressing" in reason

    def test_a_missing_heartbeat_says_so(self) -> None:
        _, reason = detect_hang(None, now=NOW, hang_after_seconds=BOUND)
        assert "no heartbeat" in reason

    def test_every_verdict_carries_a_reason(self) -> None:
        cases = [
            None,
            heartbeat("running", age=10),
            heartbeat("running", age=400),
            heartbeat("idle", age=900),
            heartbeat("backoff", age=900),
            heartbeat("nonsense"),
        ]
        for beat in cases:
            _, reason = detect_hang(beat, now=NOW, hang_after_seconds=BOUND)
            assert reason, f"no reason given for {beat!r}"

    def test_a_bound_of_zero_hangs_immediately(self) -> None:
        """The bound is a policy input, and a caller may want to disable waiting entirely."""
        assert verdict(heartbeat("running", age=1), bound=0) is HangVerdict.HUNG
