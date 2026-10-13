"""The backoff tier: retry a transient fault, halt on a defect.

The safety property is the CLASSIFICATION, not the retrying. Retrying is right for a database that
blinked and wrong for a bug: hammering a broken code path produces exactly the state the supervisor
exists to avoid -- a process that is up, is logging, and is achieving nothing -- while a brief
outage should not page anyone.

So the classifier DEFAULTS TO NEVER. An unclassified failure is permanent and halts, because
retrying a defect for ever is the worse of the two mistakes: it looks like health.
"""

from __future__ import annotations

import pytest

from tigerquant.ops.supervisor import (
    TRANSIENT_EXHAUSTED,
    BackoffPolicy,
    Supervisor,
    SupervisorConfig,
)


class DbDown(ConnectionError):
    """A stand-in for a driver's connection error."""


def policy(**kwargs: object) -> BackoffPolicy:
    base: dict[str, object] = {
        "initial_seconds": 0.001,
        "max_seconds": 0.002,
        "max_attempts": 4,
    }
    base.update(kwargs)
    return BackoffPolicy(**base)  # type: ignore[arg-type]


def config(**kwargs: object) -> SupervisorConfig:
    base: dict[str, object] = {
        "interval_seconds": 0.001,
        "max_consecutive_failures": 1,
        "backoff": policy(),
    }
    base.update(kwargs)
    return SupervisorConfig(**base)  # type: ignore[arg-type]


def transient(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionError)


class TestTheDelayMath:
    def test_it_is_exponential(self) -> None:
        # jitter_fraction=0.0: THIS TEST IS ABOUT THE CURVE, not the jitter. Asserting an exact delay
        # while jitter is on would either fail or have to accept a range, and a range stops it
        # testing that the exponential is exponential.
        p = BackoffPolicy(
            initial_seconds=1.0, multiplier=2.0, max_seconds=100.0, jitter_fraction=0.0
        )
        assert p.delay_for(1) == 1.0
        assert p.delay_for(2) == 2.0
        assert p.delay_for(3) == 4.0

    def test_it_is_capped(self) -> None:
        """Without a ceiling a long outage produces retries hours apart while the process still
        holds a heartbeat and looks healthy to everything watching it."""
        # Curve test: jitter off, same reason as above.
        p = BackoffPolicy(
            initial_seconds=1.0, multiplier=10.0, max_seconds=30.0, jitter_fraction=0.0
        )
        assert p.delay_for(5) == 30.0

    def test_a_multiplier_of_one_is_a_fixed_delay(self) -> None:
        # Curve test: a multiplier of one must stay a FIXED delay, which jitter would obscure.
        p = BackoffPolicy(
            initial_seconds=3.0, multiplier=1.0, max_seconds=30.0, jitter_fraction=0.0
        )
        assert p.delay_for(1) == p.delay_for(9) == 3.0

    def test_attempt_is_one_based(self) -> None:
        with pytest.raises(ValueError, match="1-based"):
            BackoffPolicy().delay_for(0)


class TestThePolicyRefusesNonsense:
    def test_a_zero_initial_delay_is_refused(self) -> None:
        with pytest.raises(ValueError, match="initial_seconds"):
            BackoffPolicy(initial_seconds=0)

    def test_a_shrinking_multiplier_is_refused(self) -> None:
        """A backoff that gets shorter is not a backoff, and it would hammer a sick host."""
        with pytest.raises(ValueError, match="multiplier"):
            BackoffPolicy(multiplier=0.5)

    def test_a_ceiling_below_the_start_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_seconds"):
            BackoffPolicy(initial_seconds=10.0, max_seconds=1.0)

    def test_zero_attempts_is_refused(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            BackoffPolicy(max_attempts=0)


class TestATransientFaultIsRetried:
    def test_it_recovers_without_halting(self) -> None:
        state = {"n": 0}

        def flaky() -> bool:
            state["n"] += 1
            if state["n"] <= 2:
                raise DbDown("database unreachable")
            return False

        report = Supervisor(flaky, config=config(max_cycles=4), is_transient=transient).run()
        assert not report.halted
        assert report.ok_cycles == 2
        assert report.failed_cycles == 2

    def test_the_transient_cycles_are_marked(self) -> None:
        """The distinction has to be visible in the record, or an operator cannot tell an outage
        from a defect after the fact."""
        report = Supervisor(_always_down, config=config(), is_transient=transient).run()
        assert all(outcome.transient for outcome in report.outcomes)

    def test_a_transient_blip_does_not_spend_the_permanent_budget(self) -> None:
        """Two counters, because a flapping database and a broken code path are different
        incidents. Sharing one would let a blip halt the loop for a defect that never happened."""
        state = {"n": 0}

        def blip_then_fine() -> bool:
            state["n"] += 1
            if state["n"] == 1:
                raise DbDown("blip")
            return False

        report = Supervisor(
            blip_then_fine,
            config=config(max_consecutive_failures=1, max_cycles=3),
            is_transient=transient,
        ).run()
        assert not report.halted, "a single transient failure must not trip a permanent limit of 1"


class TestAnExhaustedRetryBudgetHalts:
    def test_it_halts(self) -> None:
        """A retry loop that outlives its incident is a process nobody notices has stopped."""
        report = Supervisor(_always_down, config=config(), is_transient=transient).run()
        assert report.halted

    def test_the_reason_is_the_transient_one(self) -> None:
        """Distinct from max_consecutive_failures, because an operator acts differently on each:
        one is an outage, the other is a defect."""
        report = Supervisor(_always_down, config=config(), is_transient=transient).run()
        assert TRANSIENT_EXHAUSTED in report.halted_reason
        assert "max_consecutive_failures" not in report.halted_reason

    def test_it_stops_at_the_attempt_limit(self) -> None:
        report = Supervisor(
            _always_down, config=config(backoff=policy(max_attempts=3)), is_transient=transient
        ).run()
        assert report.cycles == 3


class TestAnUnclassifiedFailureIsPermanent:
    def test_it_halts_immediately_despite_retry_budget(self) -> None:
        """Deny-by-default, and the whole point: retrying a bug for ever looks like health."""

        def bug() -> bool:
            msg = "a defect, not an outage"
            raise ValueError(msg)

        report = Supervisor(bug, config=config(backoff=policy(max_attempts=8))).run()
        assert report.halted
        assert report.cycles == 1

    def test_no_classifier_at_all_still_halts(self) -> None:
        """The default is 'never transient', so omitting the callable is safe rather than
        accidentally permissive."""
        report = Supervisor(_always_down, config=config()).run()
        assert report.halted
        assert report.cycles == 1

    def test_the_two_halt_reasons_are_distinguishable(self) -> None:
        permanent = Supervisor(_always_value_error, config=config()).run()
        outage = Supervisor(_always_down, config=config(), is_transient=transient).run()
        assert permanent.halted_reason != outage.halted_reason

    def test_an_unclassified_failure_is_not_marked_transient(self) -> None:
        report = Supervisor(_always_value_error, config=config()).run()
        assert not report.outcomes[0].transient


class TestTheRecordStaysSerialisable:
    def test_the_transient_flag_survives_a_digest(self) -> None:
        """A float reached the canonicaliser once already in this module; the new field must not
        reintroduce one."""
        report = Supervisor(_always_down, config=config(), is_transient=transient).run()
        assert report.digest().startswith("sha256:")

    def test_it_is_json_safe(self) -> None:
        import json

        report = Supervisor(_always_down, config=config(), is_transient=transient).run()
        assert json.dumps(report.as_record())


def _always_down() -> bool:
    msg = "database unreachable"
    raise DbDown(msg)


def _always_value_error() -> bool:
    msg = "a defect"
    raise ValueError(msg)


class TestJitter:
    """The jitter's own properties. Separate from the curve, because they are separate concerns.

    Without jitter the delay is a pure function of the attempt number, so **every client that saw the
    same outage retries at the SAME INSTANTS** -- a thundering herd against a service only just coming
    back.
    """

    def test_it_is_on_by_default(self) -> None:
        """An opt-in mitigation is one nobody enables, and the herd is the default failure."""
        assert BackoffPolicy().jitter_fraction > 0

    def test_it_spreads_the_delay(self) -> None:
        plain = BackoffPolicy(jitter_fraction=0.0)
        jittered = BackoffPolicy(jitter_seed="node-a")
        assert jittered.delay_for(3) != plain.delay_for(3)

    def test_two_nodes_do_not_retry_in_lockstep(self) -> None:
        """THE point. Two supervisors with the same configuration must not retry together."""
        a = BackoffPolicy(jitter_seed="node-a")
        b = BackoffPolicy(jitter_seed="node-b")
        assert any(a.delay_for(n) != b.delay_for(n) for n in range(1, 9))

    def test_the_same_seed_replays_identically(self) -> None:
        """**TigerQuant IS DETERMINISTIC.** The audit journal must replay, so the delays have to
        reproduce -- which is why the jitter is DERIVED from a hash rather than drawn from `random`."""
        a = BackoffPolicy(jitter_seed="node-a")
        b = BackoffPolicy(jitter_seed="node-a")
        assert [a.delay_for(n) for n in range(1, 12)] == [b.delay_for(n) for n in range(1, 12)]

    def test_it_never_exceeds_the_base(self) -> None:
        """DOWNWARD ONLY, so the ceiling keeps its meaning. Jittering upward would let a retry land
        beyond `max_seconds`, and the ceiling exists because a long outage produces retries hours
        apart while the process still looks healthy."""
        plain = BackoffPolicy(jitter_fraction=0.0)
        jittered = BackoffPolicy()
        for n in range(1, 15):
            assert jittered.delay_for(n) <= plain.delay_for(n) + 1e-9

    def test_it_is_never_below_the_stated_fraction(self) -> None:
        """A delay never less than half the intended one at the default, so the backoff keeps the
        protective property it exists for."""
        plain = BackoffPolicy(jitter_fraction=0.0)
        jittered = BackoffPolicy(jitter_fraction=0.5)
        for n in range(1, 15):
            assert jittered.delay_for(n) >= plain.delay_for(n) * 0.5 - 1e-9

    def test_zero_disables_it_completely(self) -> None:
        plain = BackoffPolicy(jitter_fraction=0.0)
        for n in range(1, 8):
            assert plain.delay_for(n) == BackoffPolicy(
                initial_seconds=1.0, multiplier=2.0, jitter_fraction=0.0
            ).delay_for(n)

    def test_a_full_fraction_may_reach_near_zero(self) -> None:
        """At 1.0 the delay can approach zero. Legal, and worth pinning so the bound is understood
        rather than discovered."""
        jittered = BackoffPolicy(jitter_fraction=1.0)
        assert all(jittered.delay_for(n) >= 0 for n in range(1, 10))

    def test_it_still_rises_with_the_attempt(self) -> None:
        """Jitter must not destroy the SHAPE. Loosely checked, because jitter makes it non-monotonic
        at close attempts and the property that matters is the trend."""
        jittered = BackoffPolicy()
        assert jittered.delay_for(6) > jittered.delay_for(1)

    def test_it_respects_the_ceiling(self) -> None:
        jittered = BackoffPolicy(initial_seconds=1.0, multiplier=2.0, max_seconds=30.0)
        assert jittered.delay_for(50) <= 30.0

    @pytest.mark.parametrize("fraction", [-0.01, 1.01, 2.0])
    def test_a_fraction_outside_zero_to_one_is_refused(self, fraction: float) -> None:
        """Above 1 a retry could land before the previous one; below 0 the delay would exceed the
        ceiling. Both are refused rather than silently clamped."""
        with pytest.raises(ValueError, match="jitter_fraction"):
            BackoffPolicy(jitter_fraction=fraction)
