"""A supervised observation loop: reconciliation and drift checks on a cadence, forever.

Three things this exists to get right, each a way an unsupervised loop fails badly.

**1. A HUNG PROCESS LOOKS EXACTLY LIKE A HEALTHY ONE.** A dead process is easy: the OS notices.
A process that is alive but stuck -- blocked on a database that stopped answering, or spinning
without progressing -- looks identical to a working one from the outside. So the supervisor writes
a HEARTBEAT before and after every cycle, and the heartbeat carries the cycle index and how long
the last cycle took. An external watchdog comparing the mtime against the interval can then tell
"working" from "stuck", which a liveness check alone cannot.

**2. LOOPING FOREVER ON A BROKEN SYSTEM HIDES THE BREAKAGE.** After
``max_consecutive_failures`` cycles that raise, the supervisor HALTS and says why. The
alternative -- retrying indefinitely -- produces a process that is up, is logging, and is
reconciling nothing, which is the state most likely to be mistaken for health.

**3. IT NEVER REPAIRS ANYTHING.** It observes. The drift monitor already refuses to repair
divergence, because a divergence means we do not know which record is wrong, so overwriting either
destroys the evidence needed to find out. The supervisor inherits that stance: its only actions are
to check, to record and to alert.

**IT DOES NOT TRADE.** This is an observation loop. It has no order path, no approval registry and
no broker write credential, and the only thing it can do as a result of a cycle is raise an alert.
That boundary is the point: a long-running process is exactly where an accidental trading loop
would be most damaging and least visible.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import digest_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from tigerquant.ops.alerts import AlertRouter

__all__ = [
    "CycleOutcome",
    "Supervisor",
    "SupervisorConfig",
    "SupervisorHalted",
    "SupervisorReport",
    "heartbeat_age_seconds",
    "read_heartbeat",
]

#: Refusal, not an error: the supervisor declining to continue is a decision, and it is the one
#: action it takes on its own behalf.
HALTED_AT = "max_consecutive_failures"


class SupervisorHalted(RuntimeError):
    """Raised when the supervisor stops itself after repeated failures.

    Distinct from an ordinary exception because the caller needs to be able to tell "the loop
    stopped because the system is broken" from "the loop stopped because someone asked it to".
    """


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """How the loop behaves. Intervals are wall-clock, because this is an operational loop."""

    #: Seconds between the START of one cycle and the start of the next.
    interval_seconds: float = 30.0
    #: Consecutive failing cycles before the supervisor halts itself.
    max_consecutive_failures: int = 3
    #: Where to write the liveness record. None disables writing.
    heartbeat_path: Path | None = None
    #: Stop after this many cycles. None means run until stopped.
    max_cycles: int | None = None

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            msg = "interval_seconds must be positive"
            raise ValueError(msg)
        if self.max_consecutive_failures < 1:
            msg = "max_consecutive_failures must be at least 1"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    """What one cycle did. Kept for every cycle, not just the failures."""

    index: int
    started_at: dt.datetime
    finished_at: dt.datetime
    ok: bool
    #: Empty when ok. The exception type and message when not, so a failure is never anonymous.
    error: str = ""
    #: Whether the check reported a problem. Distinct from `ok`: a cycle that ran fine and found
    #: drift is a SUCCESS that reports bad news, and conflating the two would halt the supervisor
    #: for doing its job.
    drift_detected: bool = False
    severity: str = ""

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def as_record(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            # A STRING, not a float. The canonicaliser refuses floats on purpose -- they are
            # not reproducible across platforms -- and a digest over cycle outcomes is exactly
            # the kind of thing this project hashes. Milliseconds as an integer would also work;
            # a string is what digest_of wants and reads clearly in a JSON record.
            "duration_seconds": str(Decimal(str(round(self.duration_seconds, 6)))),
            "ok": self.ok,
            "error": self.error,
            "drift_detected": self.drift_detected,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class SupervisorReport:
    """The whole run, including why it stopped."""

    cycles: int
    ok_cycles: int
    failed_cycles: int
    #: Empty when the run ended for a planned reason (stop request or max_cycles).
    halted_reason: str = ""
    outcomes: tuple[CycleOutcome, ...] = ()
    #: The worst severity any cycle reported, so a caller sees the run's outcome at a glance.
    worst_severity: str = ""

    @property
    def halted(self) -> bool:
        return bool(self.halted_reason)

    @property
    def ok(self) -> bool:
        """A run is ok when it did not halt. Failures that recovered are not a failed run."""
        return not self.halted

    def digest(self) -> str:
        """A digest over the cycle outcomes, so two runs can be compared exactly."""
        return digest_of([outcome.as_record() for outcome in self.outcomes])

    def summary(self) -> str:
        parts = [
            f"supervisor: {self.cycles} cycle(s), {self.ok_cycles} ok, {self.failed_cycles} failed"
        ]
        if self.worst_severity:
            parts.append(f"worst severity {self.worst_severity}")
        if self.halted:
            parts.append(f"HALTED: {self.halted_reason}")
        else:
            parts.append("stopped normally")
        return "; ".join(parts)

    def as_record(self) -> dict[str, Any]:
        return {
            "cycles": self.cycles,
            "ok_cycles": self.ok_cycles,
            "failed_cycles": self.failed_cycles,
            "halted": self.halted,
            "halted_reason": self.halted_reason,
            "worst_severity": self.worst_severity,
            "digest": self.digest(),
            "outcomes": [outcome.as_record() for outcome in self.outcomes],
        }


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class Supervisor:
    """Runs one observation cycle on a cadence until stopped, or until it halts itself.

    The cycle is injected rather than imported, so this class has no dependency on the drift
    monitor, a repository or a broker. It supervises a CALLABLE, which is what makes it testable
    without a database and keeps its "does not trade" property structural rather than promised.
    """

    __slots__ = ("_alert_on_halt", "_config", "_cycle", "_outcomes", "_router", "_stopped")

    def __init__(
        self,
        cycle: Callable[[], bool | None],
        *,
        config: SupervisorConfig | None = None,
        router: AlertRouter | None = None,
    ) -> None:
        """``cycle`` returns True when it detected a problem, False or None when clean."""
        self._cycle = cycle
        self._config = config or SupervisorConfig()
        self._router = router
        self._outcomes: list[CycleOutcome] = []
        self._stopped = False
        self._alert_on_halt = True

    @property
    def config(self) -> SupervisorConfig:
        return self._config

    @property
    def outcomes(self) -> tuple[CycleOutcome, ...]:
        return tuple(self._outcomes)

    def stop(self) -> None:
        """Ask the loop to finish the current cycle and then end.

        Checked between cycles, never during one: a cycle stopped halfway could leave the
        heartbeat claiming a check happened when it did not.
        """
        self._stopped = True

    def run(self) -> SupervisorReport:
        """Run until stopped, until ``max_cycles``, or until consecutive failures force a halt."""
        index = 0
        consecutive_failures = 0
        ok_cycles = 0
        failed_cycles = 0
        worst = ""

        while not self._stopped:
            index += 1
            started = _now()
            self._write_heartbeat(index=index, started_at=started, phase="running")

            error = ""
            detected = False
            severity = ""
            try:
                detected = bool(self._cycle())
                severity = "WARNING" if detected else ""
            except Exception as exc:  # noqa: BLE001 - a cycle must not kill the supervisor
                error = f"{type(exc).__name__}: {exc}"

            finished = _now()
            outcome = CycleOutcome(
                index=index,
                started_at=started,
                finished_at=finished,
                ok=not error,
                error=error,
                drift_detected=detected,
                severity=severity,
            )
            self._outcomes.append(outcome)
            self._write_heartbeat(index=index, started_at=started, phase="idle", last_ok=outcome.ok)

            if error:
                failed_cycles += 1
                consecutive_failures += 1
            else:
                ok_cycles += 1
                # A cycle that RAN and found drift is a success. Counting it as a failure would
                # halt the supervisor for doing its job.
                consecutive_failures = 0
            if severity and severity > worst:
                worst = severity

            if consecutive_failures >= self._config.max_consecutive_failures:
                reason = (
                    f"{HALTED_AT}: {consecutive_failures} consecutive cycle(s) failed; last "
                    f"error: {error}. Halting rather than retrying: a loop that keeps running "
                    "against a broken system is the state most easily mistaken for health."
                )
                self._halt(reason)
                # The DETAIL, not just the code. Reporting HALTED_AT alone tells whoever finds
                # the process stopped THAT it halted but not what broke it -- and the error is
                # the only actionable part. A test caught this being discarded.
                return self._report(index, ok_cycles, failed_cycles, worst, halted_reason=reason)

            if self._config.max_cycles is not None and index >= self._config.max_cycles:
                break

            if self._config.interval_seconds:
                # Sleep in slices so a stop request is honoured promptly rather than after the
                # whole interval. A minute-long interval should not make Ctrl-C wait a minute.
                remaining = self._config.interval_seconds
                while remaining > 0 and not self._stopped:
                    nap = min(0.5, remaining)
                    time.sleep(nap)
                    remaining -= nap

        return self._report(index, ok_cycles, failed_cycles, worst, halted_reason="")

    # -- internals ---------------------------------------------------------------

    def _report(
        self,
        cycles: int,
        ok_cycles: int,
        failed_cycles: int,
        worst: str,
        *,
        halted_reason: str,
    ) -> SupervisorReport:
        return SupervisorReport(
            cycles=cycles,
            ok_cycles=ok_cycles,
            failed_cycles=failed_cycles,
            halted_reason=halted_reason,
            outcomes=tuple(self._outcomes),
            worst_severity=worst,
        )

    def _halt(self, reason: str) -> None:
        """Record the halt as an alert, because a stopped supervisor must not be silent."""
        if self._router is None:
            return
        from tigerquant.core.enums import Severity
        from tigerquant.ops.alerts import Alert

        self._router.route(
            [
                Alert(
                    key="supervisor:halted",
                    severity=Severity.CRITICAL,
                    title="the reconciliation supervisor halted",
                    detail=reason,
                    raised_at=_now(),
                    source="supervisor",
                    fields={"cycle": str(len(self._outcomes))},
                )
            ]
        )

    def _write_heartbeat(
        self,
        *,
        index: int,
        started_at: dt.datetime,
        phase: str,
        last_ok: bool | None = None,
    ) -> None:
        """Record that this process is alive AND progressing.

        Written twice per cycle on purpose. A single write at the end cannot distinguish "working"
        from "stuck in cycle 41", because a stuck process has simply not written yet. Writing at
        the start too means the heartbeat carries the cycle it is IN, so a watchdog can see the
        index stop advancing -- which is the actual failure it needs to detect.
        """
        path = self._config.heartbeat_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase": phase,
            "cycle": index,
            "started_at": started_at.isoformat(),
            "written_at": _now().isoformat(),
            "pid_marker": "see docs/evidence/cr09_supervisor.txt",
            "interval_seconds": self._config.interval_seconds,
        }
        if last_ok is not None:
            payload["last_cycle_ok"] = last_ok
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    """The heartbeat record, or None when absent or unreadable.

    Unreadable returns None rather than raising: a watchdog asking "is it alive" must not crash
    on a partially-written file, which is exactly what it will find mid-write.
    """
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # A heartbeat is written by this module, so a non-object here means the file was replaced or
    # corrupted. Reported as absent rather than passed on as a surprise to the watchdog.
    return loaded if isinstance(loaded, dict) else None


def heartbeat_age_seconds(path: Path) -> Decimal | None:
    """Seconds since the heartbeat was written, or None when there is no heartbeat.

    A caller compares this against the interval. Older than a few intervals means the process is
    either dead or STUCK, and the two are the same thing operationally.
    """
    record = read_heartbeat(path)
    if record is None:
        return None
    written = record.get("written_at")
    if not written:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(written))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return Decimal(str(round((_now() - moment).total_seconds(), 3)))
