"""The supervised observation loop.

Four properties, each a way an unsupervised loop fails badly:

* a STUCK process looks exactly like a healthy one, so the heartbeat carries the cycle index;
* looping for ever on a broken system hides the breakage, so it HALTS;
* a cycle that RAN and found drift is a SUCCESS, and counting it as a failure would halt the
  supervisor for doing its job;
* and it never trades -- structurally, not by promise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerquant.ops.supervisor import (
    HALTED_AT,
    Supervisor,
    SupervisorConfig,
    heartbeat_age_seconds,
    read_heartbeat,
)


def fast(**kwargs: object) -> SupervisorConfig:
    base: dict[str, object] = {"interval_seconds": 0.001, "heartbeat_path": None}
    base.update(kwargs)
    return SupervisorConfig(**base)  # type: ignore[arg-type]


class TestConfiguration:
    def test_a_zero_interval_is_refused(self) -> None:
        """A zero interval would spin, which is not a cadence."""
        with pytest.raises(ValueError, match="interval_seconds"):
            SupervisorConfig(interval_seconds=0)

    def test_zero_failure_tolerance_is_refused(self) -> None:
        """Zero would halt before the first cycle, which is not what anyone means."""
        with pytest.raises(ValueError, match="max_consecutive_failures"):
            SupervisorConfig(max_consecutive_failures=0)


class TestItHaltsOnSustainedFailure:
    def test_it_halts_after_the_configured_number_of_failures(self) -> None:
        supervisor = Supervisor(_always_raises, config=fast(max_consecutive_failures=3))
        report = supervisor.run()
        assert report.cycles == 3
        assert report.halted
        # The code identifies the rule; the detail says what broke. Both are needed.
        assert HALTED_AT in report.halted_reason

    def test_the_halt_names_the_last_error(self) -> None:
        """A halt with no reason is a mystery for whoever finds the process stopped."""
        supervisor = Supervisor(_always_raises, config=fast(max_consecutive_failures=2))
        report = supervisor.run()
        assert "database stopped answering" in report.halted_reason

    def test_a_halted_run_is_not_ok(self) -> None:
        supervisor = Supervisor(_always_raises, config=fast(max_consecutive_failures=1))
        assert supervisor.run().ok is False

    def test_every_failed_cycle_is_attributed(self) -> None:
        """An anonymous failure is one nobody can act on."""
        supervisor = Supervisor(_always_raises, config=fast(max_consecutive_failures=3))
        report = supervisor.run()
        assert all(outcome.error for outcome in report.outcomes if not outcome.ok)

    def test_the_exception_type_is_recorded_not_just_the_message(self) -> None:
        supervisor = Supervisor(_always_raises, config=fast(max_consecutive_failures=1))
        assert "RuntimeError" in supervisor.run().outcomes[0].error

    def test_it_does_not_run_a_fourth_cycle(self) -> None:
        """Halting means stopping, not reporting after one more."""
        calls = {"n": 0}

        def counted() -> bool:
            calls["n"] += 1
            msg = "nope"
            raise RuntimeError(msg)

        Supervisor(counted, config=fast(max_consecutive_failures=3)).run()
        assert calls["n"] == 3


class TestATransientFailureDoesNotHalt:
    def test_one_failure_among_successes_is_survivable(self) -> None:
        state = {"n": 0}

        def sometimes() -> bool:
            state["n"] += 1
            if state["n"] == 2:
                msg = "transient"
                raise RuntimeError(msg)
            return False

        report = Supervisor(sometimes, config=fast(max_cycles=5, max_consecutive_failures=3)).run()
        assert report.failed_cycles == 1
        assert not report.halted

    def test_the_consecutive_counter_resets_on_success(self) -> None:
        """Two failures, a success, two more: never three in a row, so no halt."""
        state = {"n": 0}

        def pattern() -> bool:
            state["n"] += 1
            if state["n"] in {1, 2, 4, 5}:
                msg = "flaky"
                raise RuntimeError(msg)
            return False

        report = Supervisor(pattern, config=fast(max_cycles=5, max_consecutive_failures=3)).run()
        assert report.failed_cycles == 4
        assert not report.halted


class TestDriftIsNotAFailure:
    def test_a_cycle_that_finds_drift_is_a_success(self) -> None:
        """Conflating "the check ran" with "the check was happy" would halt the supervisor for
        doing its job -- which is exactly backwards."""
        report = Supervisor(lambda: True, config=fast(max_cycles=4)).run()
        assert report.failed_cycles == 0
        assert report.ok_cycles == 4
        assert not report.halted

    def test_drift_is_still_reported_in_the_outcome(self) -> None:
        report = Supervisor(lambda: True, config=fast(max_cycles=1)).run()
        assert report.outcomes[0].drift_detected is True

    def test_the_worst_severity_is_summarised(self) -> None:
        report = Supervisor(lambda: True, config=fast(max_cycles=2)).run()
        assert report.worst_severity == "WARNING"

    def test_a_clean_run_has_no_severity(self) -> None:
        report = Supervisor(lambda: False, config=fast(max_cycles=2)).run()
        assert report.worst_severity == ""


class TestStopping:
    def test_a_stop_request_ends_the_loop(self) -> None:
        supervisor = Supervisor(lambda: False, config=fast())
        supervisor.stop()
        report = supervisor.run()
        assert report.cycles == 0
        assert not report.halted

    def test_a_stop_is_honoured_between_cycles_not_during_one(self) -> None:
        """A cycle stopped halfway could leave the heartbeat claiming a check happened."""
        supervisor = Supervisor(lambda: False, config=fast())
        seen: list[int] = []

        def stop_after_first() -> bool:
            seen.append(1)
            supervisor.stop()
            return False

        supervisor._cycle = stop_after_first
        report = supervisor.run()
        assert report.cycles == 1
        assert len(seen) == 1

    def test_a_planned_stop_is_not_a_halt(self) -> None:
        supervisor = Supervisor(lambda: False, config=fast(max_cycles=2))
        assert supervisor.run().halted is False


class TestTheHeartbeat:
    def test_it_is_written_when_configured(self, tmp_path: Path) -> None:
        path = tmp_path / "hb.json"
        Supervisor(lambda: False, config=fast(max_cycles=1, heartbeat_path=path)).run()
        assert path.is_file()

    def test_it_carries_the_cycle_index(self, tmp_path: Path) -> None:
        """A liveness file without a progress marker cannot distinguish working from stuck."""
        path = tmp_path / "hb.json"
        Supervisor(lambda: False, config=fast(max_cycles=3, heartbeat_path=path)).run()
        record = read_heartbeat(path)
        assert record is not None
        assert record["cycle"] == 3

    def test_it_is_written_twice_per_cycle(self, tmp_path: Path) -> None:
        """Start and end, so a watchdog can see the cycle it is IN rather than only completed
        ones -- a stuck process has simply not written the second one."""
        path = tmp_path / "hb.json"
        phases: list[str] = []
        original_write = Path.write_text

        def spy(self: Path, data: str, **kwargs: object) -> int:
            if self == path:
                import json

                phases.append(json.loads(data)["phase"])
            return original_write(self, data, **kwargs)  # type: ignore[arg-type]

        Path.write_text = spy  # type: ignore[method-assign]
        try:
            Supervisor(lambda: False, config=fast(max_cycles=2, heartbeat_path=path)).run()
        finally:
            Path.write_text = original_write  # type: ignore[method-assign]
        assert phases == ["running", "idle", "running", "idle"]

    def test_no_heartbeat_when_no_path_is_configured(self, tmp_path: Path) -> None:
        report = Supervisor(lambda: False, config=fast(max_cycles=1)).run()
        assert report.cycles == 1

    def test_the_parent_directory_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "hb.json"
        Supervisor(lambda: False, config=fast(max_cycles=1, heartbeat_path=path)).run()
        assert path.is_file()

    def test_a_missing_heartbeat_reads_as_none(self, tmp_path: Path) -> None:
        assert read_heartbeat(tmp_path / "absent.json") is None

    def test_a_corrupt_heartbeat_reads_as_none_not_an_error(self, tmp_path: Path) -> None:
        """A watchdog asking "is it alive" must not crash on a half-written file."""
        path = tmp_path / "hb.json"
        path.write_text("{not json", encoding="utf-8")
        assert read_heartbeat(path) is None

    def test_a_non_object_heartbeat_reads_as_none(self, tmp_path: Path) -> None:
        path = tmp_path / "hb.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert read_heartbeat(path) is None

    def test_the_age_of_a_fresh_heartbeat_is_near_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "hb.json"
        Supervisor(lambda: False, config=fast(max_cycles=1, heartbeat_path=path)).run()
        age = heartbeat_age_seconds(path)
        assert age is not None
        assert age < 60

    def test_the_age_of_an_absent_heartbeat_is_none(self, tmp_path: Path) -> None:
        assert heartbeat_age_seconds(tmp_path / "absent.json") is None

    def test_an_unparseable_timestamp_reads_as_none(self, tmp_path: Path) -> None:
        import json

        path = tmp_path / "hb.json"
        path.write_text(json.dumps({"written_at": "not-a-time"}), encoding="utf-8")
        assert heartbeat_age_seconds(path) is None


class TestTheReport:
    def test_it_summarises_a_normal_stop(self) -> None:
        report = Supervisor(lambda: False, config=fast(max_cycles=2)).run()
        assert "stopped normally" in report.summary()

    def test_it_shouts_about_a_halt(self) -> None:
        report = Supervisor(_always_raises, config=fast(max_consecutive_failures=1)).run()
        assert "HALTED" in report.summary()

    def test_it_is_json_serialisable(self) -> None:
        import json

        report = Supervisor(lambda: True, config=fast(max_cycles=1)).run()
        assert json.dumps(report.as_record())

    def test_the_digest_is_stable(self) -> None:
        report = Supervisor(lambda: True, config=fast(max_cycles=2)).run()
        assert report.digest() == report.digest()


class TestItDoesNotTrade:
    def test_the_cycle_is_injected_so_there_is_no_broker_dependency(self) -> None:
        """Structural, not promised: the class supervises a CALLABLE, so it cannot reach a venue
        even if someone wanted it to."""
        import inspect

        from tigerquant.ops import supervisor as module

        source = inspect.getsource(module)
        for forbidden in ("submit_order", "place_order", "OrderIntent", "place_"):
            assert forbidden not in source, f"{forbidden} must not appear in an observation loop"

    def test_it_imports_no_broker_adapter(self) -> None:
        import inspect

        from tigerquant.ops import supervisor as module

        source = inspect.getsource(module)
        assert "tigerquant.adapters" not in source

    def test_the_module_docstring_states_the_boundary(self) -> None:
        from tigerquant.ops import supervisor as module

        assert "DOES NOT TRADE" in (module.__doc__ or "")


def _always_raises() -> bool:
    msg = "database stopped answering"
    raise RuntimeError(msg)
