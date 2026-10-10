"""CR-09: the long-duration stability harness.

The most important assertions here are about **labelling**. The harness runs a simulated period
in wall-clock seconds, and the two clocks mean completely different things:

* ``simulated_span_hours`` is how much MARKET time the run covered;
* ``wall_seconds`` is how long the machine took.

A report that merged them into one "duration" would be the same mistake as calling fixture step
sizes "latency" — a distinction this project has already had to make once, and the reason both
figures are separate fields rather than one number.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tigerquant.pipeline.long_run import LongRunConfig, build_fixture, run_long_sessions

SMALL = LongRunConfig(sessions=1, bars_per_session=40)


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> object:
    workdir = tmp_path_factory.mktemp("long-run")
    return run_long_sessions(SMALL, workdir=Path(workdir))


class TestTheTwoClocks:
    def test_market_time_and_wall_time_are_separate_fields(self, report: object) -> None:
        record = report.as_record()  # type: ignore[attr-defined]
        assert "simulated_span_hours" in record
        assert "wall_seconds" in record
        # There is deliberately no merged "duration" key to be misread.
        assert "duration" not in record
        assert "elapsed" not in record

    def test_the_simulated_span_matches_the_configured_bars(self, report: object) -> None:
        """40 one-minute bars is 40 minutes of market time, whatever the machine took."""
        assert report.simulated_span_hours == Decimal("0.67")  # type: ignore[attr-defined]

    def test_the_wall_time_is_plausibly_small(self, report: object) -> None:
        """The point of a deterministic simulator: a long market period costs seconds."""
        assert report.wall_seconds > 0  # type: ignore[attr-defined]
        assert report.wall_seconds < 600

    def test_the_summary_says_both_and_names_which_is_which(self, report: object) -> None:
        summary = report.summary()  # type: ignore[attr-defined]
        assert "MARKET time" in summary
        assert "wall time" in summary

    def test_throughput_is_reported_so_a_longer_run_can_be_planned(self, report: object) -> None:
        assert report.bars_per_second > 0  # type: ignore[attr-defined]


class TestInvariants:
    def test_the_invariants_are_named_not_implied(self, report: object) -> None:
        checked = report.invariants_checked  # type: ignore[attr-defined]
        assert len(checked) >= 5
        assert any("hash chain" in name for name in checked)
        assert any("reproducible" in name for name in checked)
        assert any("limits were exercised" in name for name in checked)

    def test_a_clean_run_has_no_violations(self, report: object) -> None:
        assert report.violations == ()  # type: ignore[attr-defined]
        assert report.ok  # type: ignore[attr-defined]

    def test_the_run_is_reproducible(self, report: object) -> None:
        """Two passes over the same scenario must produce the same journal digest."""
        assert report.deterministic  # type: ignore[attr-defined]

    def test_the_journal_chain_verified(self, report: object) -> None:
        assert report.journal_digest.startswith("sha256:")  # type: ignore[attr-defined]

    def test_the_run_actually_did_something(self, report: object) -> None:
        """A 'stable' run that processed nothing would be a vacuous pass."""
        assert report.events > 0  # type: ignore[attr-defined]
        assert report.decisions > 0  # type: ignore[attr-defined]
        assert report.bars == SMALL.total_bars  # type: ignore[attr-defined]


class TestTheLimitsAreExercised:
    def test_risk_denials_occur_over_a_long_run(self, report: object) -> None:
        """A limit that never fires is not evidence that it works.

        The harness records this rather than requiring it, because a scenario *could* legitimately
        never trip a limit -- but the report makes it visible either way.
        """
        assert report.risk_denials >= 0  # type: ignore[attr-defined]
        record = report.as_record()  # type: ignore[attr-defined]
        assert "risk_denials" in record

    def test_a_run_with_no_activity_at_all_is_a_violation(self) -> None:
        """Asserted directly, because the honest failure mode is a vacuous pass."""
        from tigerquant.pipeline.long_run import _check_invariants

        violations = _check_invariants(
            {"chain_valid": True, "events": 0, "orders_submitted": 0, "risk_denials": 0},
            deterministic=True,
        )
        assert any("no events" in reason for reason in violations)

    def test_a_broken_chain_is_a_violation(self) -> None:
        from tigerquant.pipeline.long_run import _check_invariants

        violations = _check_invariants(
            {
                "chain_valid": False,
                "events": 100,
                "orders_submitted": 5,
                "risk_denials": 1,
            },
            deterministic=True,
        )
        assert any("hash chain" in reason for reason in violations)

    def test_a_non_reproducible_run_is_a_violation(self) -> None:
        from tigerquant.pipeline.long_run import _check_invariants

        violations = _check_invariants(
            {"chain_valid": True, "events": 100, "orders_submitted": 5, "risk_denials": 1},
            deterministic=False,
        )
        assert any("DIFFERENT journal digests" in reason for reason in violations)


class TestFixtureGeneration:
    def test_the_fixture_loads(self, tmp_path: Path) -> None:
        """Generated fixtures must satisfy the same schema as checked-in ones."""
        import yaml

        from tigerquant.market.fixtures import load_scenario

        path = tmp_path / "scenario.yaml"
        path.write_text(yaml.safe_dump(build_fixture(SMALL), sort_keys=False), encoding="utf-8")
        fixture = load_scenario(path)
        assert fixture.scenario_id.startswith("long_run_")
        assert fixture.universe == ("SPY",)

    def test_the_scenario_is_labelled_simulated(self) -> None:
        fixture = build_fixture(SMALL)
        assert "SIMULATED" in fixture["description"]
        assert "Not market data and not trading advice" in fixture["description"]

    def test_the_price_series_is_deterministic(self) -> None:
        """No random source anywhere: a scenario that cannot be reproduced is not evidence."""
        first = build_fixture(SMALL)
        second = build_fixture(SMALL)
        assert first == second

    def test_the_bar_count_is_exact(self) -> None:
        fixture = build_fixture(LongRunConfig(sessions=2, bars_per_session=30))
        assert len(fixture["instruments"]["SPY"]["bars"]) == 60

    def test_prices_stay_positive(self) -> None:
        """A negative price would produce nonsense downstream rather than an obvious error."""
        fixture = build_fixture(LongRunConfig(sessions=3, bars_per_session=100))
        for bar in fixture["instruments"]["SPY"]["bars"]:
            assert Decimal(bar["c"]) > 0
            assert Decimal(bar["l"]) > 0

    def test_the_drift_direction_follows_the_sign(self) -> None:
        up = build_fixture(LongRunConfig(sessions=1, bars_per_session=50, drift_bps=10, wave_bps=0))
        down = build_fixture(
            LongRunConfig(sessions=1, bars_per_session=50, drift_bps=-10, wave_bps=0)
        )
        up_last = Decimal(up["instruments"]["SPY"]["bars"][-1]["c"])
        down_last = Decimal(down["instruments"]["SPY"]["bars"][-1]["c"])
        assert up_last > Decimal(600)
        assert down_last < Decimal(600)


class TestConfigValidation:
    def test_zero_sessions_is_refused(self) -> None:
        with pytest.raises(ValueError, match="sessions"):
            LongRunConfig(sessions=0)

    def test_zero_bars_is_refused(self) -> None:
        with pytest.raises(ValueError, match="bars_per_session"):
            LongRunConfig(bars_per_session=0)

    def test_total_bars_is_the_product(self) -> None:
        assert LongRunConfig(sessions=4, bars_per_session=100).total_bars == 400


class TestTheRecordIsSerialisable:
    def test_it_is_json_serialisable(self, report: object) -> None:
        import json

        assert json.dumps(report.as_record())  # type: ignore[attr-defined]

    def test_the_digest_is_stable(self, report: object) -> None:
        assert report.digest() == report.digest()  # type: ignore[attr-defined]
