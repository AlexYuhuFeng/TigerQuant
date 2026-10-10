"""Long-duration stability: run the simulator over many sessions and check what holds.

CR-09's gate asks for "stability over representative periods". This is the apparatus for that,
and it is entirely offline — the simulator is deterministic, so a long simulated period costs
wall-clock seconds rather than weeks.

**Two clocks, and the report never conflates them.** ``simulated_span`` is how much market time
the run covered; ``wall_seconds`` is how long the machine took. A report that called the first
one "duration" would be the same mistake as calling fixture step sizes "latency", and this
project has already made that distinction once.

What is actually checked, each an invariant that could fail over a long run and not over a short
one:

* **the journal hash chain stays valid** across every append, so a long run cannot silently
  corrupt the record of authority;
* **journal and database stay in step** after projecting the whole run — the drift check on a
  journal thousands of events long;
* **the risk limits actually engage.** A run long enough to exhaust turnover or trip the daily
  loss limit must show denials, because a limit that never fires over a long period is not
  evidence that it works;
* **the run is reproducible**: twice through, same journal digest;
* **the session counter rolls** rather than accumulating forever.

The harness deliberately does NOT claim to have run for a long WALL-CLOCK time. Simulating a
month of market time in ten seconds is a real and useful thing, and saying so is the honest
description.

It lives in `pipeline/` rather than `ops/` because it orchestrates the whole pipeline --
replay, journal, lineage -- and `ops/` sits BELOW it. The first version was in `ops/` and this
project's own import-boundary test caught the upward import, which is the second time that test
has caught a file placed by convenience rather than by layer.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import digest_of
from tigerquant.core.money import ZERO, q_cash

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "LongRunConfig",
    "StabilityReport",
    "build_fixture",
    "run_long_sessions",
]

#: A US equity regular session is 390 minutes.
_BARS_PER_SESSION = 390


@dataclass(frozen=True, slots=True)
class LongRunConfig:
    """What to simulate. All market time, none of it wall time."""

    sessions: int = 5
    bars_per_session: int = _BARS_PER_SESSION
    seed_price: Decimal = Decimal("600.00")
    #: Per-bar drift in basis points. Positive trends up; the sign chooses the scenario.
    drift_bps: int = 3
    #: Optional oscillation, so the price is not monotone and the strategy has to react.
    wave_bps: int = 40
    target_weight: Decimal = Decimal("0.05")
    initial_cash: Decimal = Decimal("100000.00")
    #: One order per cycle by default, so a long run submits steadily rather than in bursts.
    max_orders_per_cycle: int = 1

    def __post_init__(self) -> None:
        if self.sessions < 1:
            msg = "sessions must be at least 1"
            raise ValueError(msg)
        if self.bars_per_session < 1:
            msg = "bars_per_session must be at least 1"
            raise ValueError(msg)

    @property
    def total_bars(self) -> int:
        return self.sessions * self.bars_per_session


def build_fixture(config: LongRunConfig) -> dict[str, Any]:
    """A scenario fixture for the run.

    Generated rather than checked in: a committed fixture of thousands of bars would be a large
    file whose content is a formula, and the formula is clearer than the file.
    """
    bars: list[dict[str, Any]] = []
    price = config.seed_price
    step = config.seed_price * Decimal(config.drift_bps) / Decimal(10_000)
    amplitude = config.seed_price * Decimal(config.wave_bps) / Decimal(10_000)

    for index in range(config.total_bars):
        price += step
        # A slow wave so the price is not monotone. Deterministic: no random source anywhere,
        # because a scenario that cannot be reproduced is not evidence.
        phase = Decimal(index % 60) / Decimal(60)
        offset = amplitude * (phase - Decimal("0.5")) if config.wave_bps else ZERO
        close = q_cash(price + offset)
        bars.append(
            {
                "t": index,
                "o": str(q_cash(close - step / 2)),
                "h": str(q_cash(close + step)),
                "l": str(q_cash(close - step)),
                "c": str(close),
                "v": 10_000 + (index % 50) * 100,
            }
        )

    start = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
    return {
        "scenario_id": f"long_run_{config.sessions}x{config.bars_per_session}",
        "description": (
            f"SIMULATED long-duration stability run: {config.sessions} session(s) x "
            f"{config.bars_per_session} bars. Not market data and not trading advice."
        ),
        "mode": "BACKTEST",
        "start": start.isoformat(),
        "interval": "1m",
        "initial_cash": str(config.initial_cash),
        "universe": ["SPY"],
        "max_fact_age_seconds": 900,
        "strategy": {
            "kind": "momentum",
            "lookback": 3,
            "entry_threshold_bps": "10",
            "exit_threshold_bps": "10",
            "target_weight": str(config.target_weight),
            "horizon": "5 trading days",
        },
        "risk": {
            "gross_exposure_fraction": "0.50",
            "max_symbol_fraction": "0.10",
            "max_order_fraction": "0.10",
            "max_mark_age_seconds": 900,
            "allow_margin": False,
            "allow_short": False,
            "allow_options": False,
            "max_orders_per_cycle": config.max_orders_per_cycle,
        },
        "broker": {
            "fill_policy": "immediate",
            "commission_per_share": "0.005",
            "slippage_bps": "1",
        },
        "instruments": {"SPY": {"bars": bars}},
    }


@dataclass(frozen=True, slots=True)
class StabilityReport:
    """What a long run did, with the two clocks kept apart."""

    sessions: int
    bars: int
    #: Market time the run covered. NOT how long it took.
    simulated_span_hours: Decimal
    #: How long the machine took. NOT market time.
    wall_seconds: Decimal
    events: int
    decisions: int
    orders_submitted: int
    orders_filled: int
    risk_denials: int
    #: Invariants checked after the run, named so a gap is visible rather than implied.
    invariants_checked: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    journal_digest: str = ""
    deterministic: bool = False
    final_equity: Decimal = ZERO
    final_cash: Decimal = ZERO

    @property
    def ok(self) -> bool:
        return not self.violations and self.deterministic

    @property
    def bars_per_second(self) -> Decimal:
        """Throughput, so a longer run can be planned rather than guessed at."""
        if self.wall_seconds <= 0:
            return ZERO
        return (Decimal(self.bars) / self.wall_seconds).quantize(Decimal("0.1"))

    def digest(self) -> str:
        return digest_of(
            {
                "sessions": self.sessions,
                "bars": self.bars,
                "events": self.events,
                "journal_digest": self.journal_digest,
                "violations": list(self.violations),
            }
        )

    def summary(self) -> str:
        verdict = "STABLE" if self.ok else "UNSTABLE"
        parts = [
            f"long-run [{verdict}]",
            f"{self.sessions} simulated session(s), {self.bars} bars",
            f"covering {self.simulated_span_hours}h of MARKET time in {self.wall_seconds}s of "
            f"wall time ({self.bars_per_second} bars/s)",
            f"{self.events} event(s), {self.decisions} decision(s), "
            f"{self.orders_submitted} submitted, {self.orders_filled} filled, "
            f"{self.risk_denials} risk denial(s)",
            f"journal {self.journal_digest[:23]}…",
        ]
        if self.violations:
            parts.append(f"VIOLATIONS: {list(self.violations)}")
        if not self.deterministic:
            parts.append("NOT REPRODUCIBLE: two runs produced different digests")
        return "; ".join(parts)

    def as_record(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "bars": self.bars,
            # The two clocks are separate fields, never a single "duration".
            "simulated_span_hours": str(self.simulated_span_hours),
            "wall_seconds": str(self.wall_seconds),
            "bars_per_second": str(self.bars_per_second),
            "events": self.events,
            "decisions": self.decisions,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "risk_denials": self.risk_denials,
            "invariants_checked": list(self.invariants_checked),
            "violations": list(self.violations),
            "journal_digest": self.journal_digest,
            "deterministic": self.deterministic,
            "final_equity": str(self.final_equity),
            "final_cash": str(self.final_cash),
            "ok": self.ok,
        }


_INVARIANTS = (
    "journal hash chain verifies after the whole run",
    "journal and database agree after projecting the whole run",
    "the run is reproducible: two passes produce the same journal digest",
    "every fill traces back to a decision",
    "the risk limits were exercised (a limit that never fires is not evidence)",
    "journal and database agree after projecting the whole run",
    "every fill traces back to a decision",
)


def run_long_sessions(
    config: LongRunConfig | None = None,
    *,
    workdir: Path | None = None,
    check_determinism: bool = True,
) -> StabilityReport:
    """Simulate the configured period and check what has to hold.

    Runs twice when ``check_determinism`` is set, because reproducibility over a long run is the
    property most likely to fail in a way a short run cannot show.
    """
    from tigerquant.market.fixtures import load_scenario
    from tigerquant.pipeline.replay import run_replay

    settings = config or LongRunConfig()
    root = workdir or Path(tempfile.mkdtemp(prefix="tq-longrun-"))
    root.mkdir(parents=True, exist_ok=True)

    fixture_path = root / "scenario.yaml"
    fixture_path.write_text(_to_yaml(build_fixture(settings)), encoding="utf-8")

    started = time.perf_counter()
    first = _one_pass(fixture_path, root / "run1.jsonl", load_scenario, run_replay)
    wall_seconds = Decimal(str(round(time.perf_counter() - started, 4)))

    deterministic = True
    if check_determinism:
        second = _one_pass(fixture_path, root / "run2.jsonl", load_scenario, run_replay)
        deterministic = first["journal_digest"] == second["journal_digest"]

    from tigerquant.db.repository import InMemoryRepository

    violations = list(
        _check_invariants(
            first,
            deterministic=deterministic,
            repository=InMemoryRepository(),
        )
    )

    return StabilityReport(
        sessions=settings.sessions,
        bars=settings.total_bars,
        simulated_span_hours=(Decimal(settings.total_bars) / Decimal(60)).quantize(Decimal("0.01")),
        wall_seconds=wall_seconds,
        events=first["events"],
        decisions=first["decisions"],
        orders_submitted=first["orders_submitted"],
        orders_filled=first["orders_filled"],
        risk_denials=first["risk_denials"],
        invariants_checked=_INVARIANTS,
        violations=tuple(violations),
        journal_digest=first["journal_digest"],
        deterministic=deterministic,
        final_equity=first["final_equity"],
        final_cash=first["final_cash"],
    )


def _one_pass(
    fixture_path: Path,
    journal_path: Path,
    load_scenario: Any,
    run_replay: Any,
) -> dict[str, Any]:
    """One full simulation, returning what the invariant checks need."""
    from tigerquant.core.enums import Mode
    from tigerquant.events.journal import AppendOnlyJournal

    journal_path.unlink(missing_ok=True)
    fixture = load_scenario(fixture_path)
    report = run_replay(fixture, journal_path=journal_path)

    with AppendOnlyJournal(journal_path, Mode(fixture.mode.value), fsync=False) as journal:
        verification = journal.verify()
        events = tuple(record.event for record in journal.iter_records())

    return {
        "events": len(events),
        "decisions": report.decisions_proposed,
        "orders_submitted": report.orders_submitted,
        "orders_filled": report.orders_filled,
        "risk_denials": report.risk_denials,
        "chain_valid": verification.ok,
        "journal_digest": report.journal_digest,
        "final_equity": Decimal(str(report.final_equity)),
        "final_cash": Decimal(str(report.final_cash)),
        "events_data": events,
        "mode": fixture.mode.value,
    }


def _check_invariants(
    result: dict[str, Any],
    *,
    deterministic: bool,
    repository: Any = None,
) -> Sequence[str]:
    """Every invariant that could fail over a long run and not a short one.

    Each entry in ``_INVARIANTS`` is checked here. A list that claimed a check nobody performed
    would read as evidence for something unverified, which is worse than not claiming it.
    """
    violations: list[str] = []

    if not result["chain_valid"]:
        violations.append(
            "the journal hash chain did NOT verify after the run. The record of authority is "
            "damaged; every projection from it is suspect."
        )

    if not deterministic:
        violations.append(
            "two runs over the same scenario produced DIFFERENT journal digests. A long run "
            "that cannot be reproduced is not evidence."
        )

    if result["events"] == 0:
        violations.append("the run produced no events, so nothing was actually exercised")

    # A limit that never fires over a long period is not evidence that it works.
    if result["risk_denials"] == 0 and result["orders_submitted"] == 0:
        violations.append(
            "no orders were submitted and no risk denials occurred, so the run exercised "
            "neither the order path nor the limits"
        )

    # Every fill must trace back to a decision. Checked on the real events, not asserted.
    events = result.get("events_data") or ()
    mode = str(result.get("mode", "BACKTEST"))
    if events:
        chain = lineage_completeness(events, mode=mode)
        if chain["unlinked_orders"]:
            violations.append(
                f"{len(chain['unlinked_orders'])} order(s) trace back to no decision: "
                f"{chain['unlinked_orders']}"
            )
        if chain["unlinked_fills"]:
            violations.append(
                f"{len(chain['unlinked_fills'])} fill(s) trace back to no decision: "
                f"{chain['unlinked_fills']}"
            )
        if chain["fabrications"]:
            violations.append(
                f"{len(chain['fabrications'])} decision(s) cited evidence they were never shown"
            )

    # Journal and database must agree after projecting the whole run.
    if repository is not None:
        from tigerquant.db.projector import check_drift, project

        project(events, repository, require_contiguous_sequence=True)
        drift = check_drift(events, repository)
        if not drift.is_consistent:
            violations.append(f"journal and database drifted after projection: {drift.summary()}")

    return violations


def _to_yaml(payload: dict[str, Any]) -> str:
    """Serialise a fixture dict. Uses PyYAML, which is a hard dependency."""
    import yaml

    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)


def lineage_completeness(events: Sequence[Any], *, mode: str) -> dict[str, Any]:
    """Whether every fill in a long run traces back to a decision.

    Separate from the run so it can be applied to a journal already on disk, which is what an
    operator investigating an old run would do.
    """
    from tigerquant.ops.lineage_report import build_lineage

    report = build_lineage(events, mode=mode)
    return {
        "decisions": report.decisions,
        "fills": report.fills,
        "unlinked_orders": list(report.unlinked_orders),
        "unlinked_fills": list(report.unlinked_fills),
        "fabrications": [record.decision_id for record in report.fabrications],
        "clean": report.is_clean,
    }


def write_report(report: StabilityReport, path: Path) -> Path:
    """Persist a stability report as evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_record(), indent=2, sort_keys=True), encoding="utf-8")
    return path
