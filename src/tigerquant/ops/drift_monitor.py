"""Periodic journal-versus-database drift monitoring, with alerting that stays readable.

CR-09's gate asks for "measured false alerts". That phrase is a design constraint, not a
reporting one. A drift monitor that emitted one alert per tick would produce thousands of
identical alerts for a single unresolved problem, and the operator would stop reading them —
which is the same as having no monitor at all. So this module alerts on **transitions**:

* the first time drift is detected, at ``WARNING``;
* nothing further while the same drift persists, except one reminder per
  ``reminder_interval_seconds``, so a long outage is not silent;
* an **escalation to CRITICAL** once the drift has survived ``escalate_after`` consecutive
  checks, because a drift that does not resolve itself is a different situation from one that
  appears and clears;
* a **recovery** alert when it clears, so the operator knows without re-checking.

Each alert carries ``occurrences`` — how many checks have seen this same drift — so the
false-alert rate can be measured from the journal afterwards rather than guessed at.

Two independent consequences, deliberately separate:

* the monitor **emits** an alert;
* the monitor **reports** whether new exposure should stop. It does not stop it itself. The
  watchdog owns that decision, and giving the monitor the power to halt would create a second
  place where a halt can be triggered and a second place for it to be wrong.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import ReasonCode, Severity
from tigerquant.db.projector import check_drift

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from tigerquant.core.clock import Clock
    from tigerquant.db.projector import DriftReport
    from tigerquant.db.repository import Repository
    from tigerquant.events.base import EventBase
    from tigerquant.events.emitter import EventEmitter

__all__ = [
    "DEFAULT_DRIFT_POLICY",
    "DriftAlertPolicy",
    "DriftCheckResult",
    "DriftMonitor",
]


@dataclass(frozen=True, slots=True)
class DriftAlertPolicy:
    """When a persistent drift should produce another alert."""

    #: Re-alert at most this often while the same drift persists.
    reminder_interval_seconds: int = 900
    #: Escalate to CRITICAL after this many consecutive drifted checks.
    escalate_after: int = 3
    #: Whether a drift should block new exposure. The monitor only *reports* this.
    blocks_new_exposure: bool = True

    def __post_init__(self) -> None:
        if self.reminder_interval_seconds < 1:
            msg = "reminder_interval_seconds must be at least 1"
            raise ValueError(msg)
        if self.escalate_after < 1:
            msg = "escalate_after must be at least 1"
            raise ValueError(msg)


DEFAULT_DRIFT_POLICY = DriftAlertPolicy()


@dataclass(frozen=True, slots=True)
class DriftCheckResult:
    """One drift check and what it decided."""

    checked_at: dt.datetime
    consistent: bool
    report: DriftReport
    severity: Severity | None
    occurrences: int
    escalated: bool
    alerted: bool
    recovered: bool
    detail: str

    @property
    def blocks_new_exposure(self) -> bool:
        return not self.consistent

    def describe(self) -> str:
        if self.consistent and self.recovered:
            return f"recovered at {self.checked_at.isoformat()}: journal and database agree again"
        if self.consistent:
            return f"consistent at {self.checked_at.isoformat()}"
        return (
            f"DRIFT (occurrence {self.occurrences}, {self.severity.value if self.severity else '?'}) "
            f"at {self.checked_at.isoformat()}: {self.detail}"
        )

    def as_record(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "consistent": self.consistent,
            "severity": self.severity.value if self.severity else None,
            "occurrences": self.occurrences,
            "escalated": self.escalated,
            "alerted": self.alerted,
            "recovered": self.recovered,
            "blocks_new_exposure": self.blocks_new_exposure,
            "detail": self.detail,
        }


@dataclass
class _MonitorState:
    consecutive_drifted: int = 0
    last_alert_at: dt.datetime | None = None
    was_drifted: bool = False


@dataclass
class DriftMonitor:
    """Checks the journal against the database and alerts on transitions.

    Holds no clock of its own: the instant of a check is passed in, so a test can drive a
    year of checks in a millisecond and a replay reproduces exactly which alerts fired.
    """

    repository: Repository
    #: Where alerts go. Optional, because a read-only inspection is a legitimate use: a
    #: caller who only wants to know whether the two agree should not have to open the
    #: journal for append to find out. When it is None the monitor still reports, records
    #: its history and measures its own alert rate; it simply emits nothing.
    emitter: EventEmitter | None = None
    policy: DriftAlertPolicy = DEFAULT_DRIFT_POLICY
    _state: _MonitorState = field(default_factory=_MonitorState, repr=False)
    _history: list[DriftCheckResult] = field(default_factory=list, repr=False)

    @property
    def history(self) -> tuple[DriftCheckResult, ...]:
        return tuple(self._history)

    @property
    def consecutive_drifted(self) -> int:
        return self._state.consecutive_drifted

    @property
    def is_drifted(self) -> bool:
        """Whether the last check found drift. The watchdog reads this, not the alerts."""
        return self._state.was_drifted

    def check(
        self,
        journal_events: Sequence[EventBase],
        *,
        at: dt.datetime,
        correlation_id: str | None = None,
    ) -> DriftCheckResult:
        """Compare the journal with the database and alert if the situation changed."""
        moment = ensure_utc(at)
        report = check_drift(journal_events, self.repository)
        consistent = report.is_consistent

        if consistent:
            recovered = self._state.was_drifted
            self._state.consecutive_drifted = 0
            self._state.was_drifted = False
            if recovered:
                self._emit(
                    severity=Severity.INFO,
                    code=ReasonCode.UNRECONCILED,
                    message=(
                        "journal and database agree again after a drift. The drift was not "
                        "repaired by this monitor: confirm what resolved it before trusting "
                        "the projections."
                    ),
                    occurrences=1,
                    at=moment,
                    correlation_id=correlation_id,
                )
            result = DriftCheckResult(
                checked_at=moment,
                consistent=True,
                report=report,
                severity=None,
                occurrences=0,
                escalated=False,
                alerted=recovered,
                recovered=recovered,
                detail=report.summary(),
            )
            self._history.append(result)
            return result

        self._state.consecutive_drifted += 1
        occurrences = self._state.consecutive_drifted
        was_drifted = self._state.was_drifted
        self._state.was_drifted = True

        escalated = occurrences >= self.policy.escalate_after and (
            self._state.last_alert_at is None or occurrences == self.policy.escalate_after
        )
        severity = (
            Severity.CRITICAL if occurrences >= self.policy.escalate_after else Severity.WARNING
        )

        due = self._state.last_alert_at is None or (
            (moment - self._state.last_alert_at).total_seconds()
            >= self.policy.reminder_interval_seconds
        )
        # Alert on the first drift, on escalation, and then only on the reminder cadence.
        alerted = not was_drifted or escalated or due

        if alerted:
            self._emit(
                severity=severity,
                code=ReasonCode.UNRECONCILED,
                message=(
                    f"journal/database drift (occurrence {occurrences}): {report.summary()} "
                    "The monitor never repairs drift: a divergence means we do not know which "
                    "record is wrong, so overwriting either would destroy the evidence needed "
                    "to find out."
                ),
                occurrences=occurrences,
                at=moment,
                correlation_id=correlation_id,
            )
            self._state.last_alert_at = moment

        result = DriftCheckResult(
            checked_at=moment,
            consistent=False,
            report=report,
            severity=severity,
            occurrences=occurrences,
            escalated=escalated,
            alerted=alerted,
            recovered=False,
            detail=report.summary(),
        )
        self._history.append(result)
        return result

    def _emit(
        self,
        *,
        severity: Severity,
        code: ReasonCode,
        message: str,
        occurrences: int,
        at: dt.datetime,
        correlation_id: str | None,
    ) -> None:
        if self.emitter is None:
            # A read-only inspection. The monitor still reports, keeps its history and
            # measures its own alert rate; it simply records nothing.
            return
        from tigerquant.events.risk import AlertRaised

        self.emitter.emit(
            AlertRaised,
            correlation_id=correlation_id,
            occurred_at=at,
            severity=severity,
            code=code,
            message=message,
            occurrences=occurrences,
            context={"consecutive_drifted": occurrences, "monitor": "drift"},
        )

    # -- measurement -------------------------------------------------------------

    def alert_rate(self) -> dict[str, float]:
        """Measured alert behaviour, so "false alerts" is a number rather than an opinion.

        ``alerts_per_drifted_check`` near 1.0 means the monitor is effectively shouting every
        time; near 0 means it is silent about a real problem. Both are failures, and the only
        way to know which one you have is to count.
        """
        drifted = [result for result in self._history if not result.consistent]
        alerts = sum(1 for result in drifted if result.alerted)
        return {
            "checks": float(len(self._history)),
            "drifted_checks": float(len(drifted)),
            "alerts": float(alerts),
            "alerts_per_drifted_check": (alerts / len(drifted)) if drifted else 0.0,
            "alerts_per_check": (alerts / len(self._history)) if self._history else 0.0,
        }

    def summarise(self) -> str:
        rates = self.alert_rate()
        state = "DRIFTED" if self.is_drifted else "consistent"
        return (
            f"drift monitor: {state}, {int(rates['checks'])} check(s), "
            f"{int(rates['alerts'])} alert(s), "
            f"{rates['alerts_per_drifted_check']:.3f} alerts per drifted check"
        )


def interval_runner(
    monitor: DriftMonitor,
    *,
    clock: Clock,
    journal_loader: Callable[[], Sequence[EventBase]],
    interval_seconds: int,
    iterations: int,
) -> tuple[DriftCheckResult, ...]:
    """Run the monitor on an injected clock, without sleeping.

    A real scheduler would sleep. This takes the clock as a dependency and advances it
    itself, so a test can run a day of checks instantly and a replay can reproduce which
    instants were checked. The deployment scheduler calls :meth:`DriftMonitor.check` directly;
    this exists so the cadence logic is testable rather than assumed.
    """
    results: list[DriftCheckResult] = []
    for _ in range(iterations):
        results.append(monitor.check(journal_loader(), at=clock.now()))
        if hasattr(clock, "advance"):
            clock.advance(dt.timedelta(seconds=interval_seconds))
    return tuple(results)


def drift_ratio(report: DriftReport) -> Decimal:
    """Fraction of journal events missing from the database, for a threshold or a chart."""
    if report.journal_events == 0:
        return Decimal(0)
    return (Decimal(len(report.missing_event_ids)) / Decimal(report.journal_events)).quantize(
        Decimal("0.0001")
    )
