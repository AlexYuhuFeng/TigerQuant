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
import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import digest_of
from tigerquant.core.clock import ensure_utc

if TYPE_CHECKING:
    from collections.abc import Callable

    from tigerquant.ops.alerts import AlertRouter

__all__ = [
    "TRANSIENT_EXHAUSTED",
    "BackoffPolicy",
    "CycleOutcome",
    "HangVerdict",
    "Supervisor",
    "SupervisorConfig",
    "SupervisorHalted",
    "SupervisorReport",
    "detect_hang",
    "heartbeat_age_seconds",
    "read_heartbeat",
]

#: Refusal, not an error: the supervisor declining to continue is a decision, and it is the one
#: action it takes on its own behalf.
HALTED_AT = "max_consecutive_failures"

#: The OTHER halt reason, kept distinct because the two mean different things operationally:
#: "max_consecutive_failures" is a defect, "transient_exhausted" is an outage that outlasted
#: the retry budget. An operator acts differently on each.
TRANSIENT_EXHAUSTED = "transient_exhausted"


class SupervisorHalted(RuntimeError):
    """Raised when the supervisor stops itself after repeated failures.

    Distinct from an ordinary exception because the caller needs to be able to tell "the loop
    stopped because the system is broken" from "the loop stopped because someone asked it to".
    """


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """How long to wait after a TRANSIENT failure, and when to stop waiting.

    Exponential with a ceiling, rather than a fixed delay, because the two failure modes have
    different right answers: a database restarting wants a short first retry, and a network
    partition wants the retries to thin out rather than hammer a host that is already unwell.
    """

    #: Wait after the first transient failure.
    initial_seconds: float = 1.0
    multiplier: float = 2.0
    #: Ceiling. Without one, a long outage produces retries hours apart while the process still
    #: holds a connection and a heartbeat, which looks healthy to everything watching it.
    max_seconds: float = 60.0
    #: Consecutive TRANSIENT failures before giving up and halting.
    #:
    #: Bounded on purpose. "Retry for ever" is the one policy that guarantees the process outlives
    #: the incident it is supposed to report, and nobody ever notices it stopped working.
    max_attempts: int = 8

    #: Fraction of the computed delay that may be shaved off to de-synchronise retries.
    #:
    #: WITHOUT THIS, EVERY CLIENT THAT SAW THE SAME OUTAGE RETRIES AT THE SAME INSTANTS, so a service
    #: coming back is hit by all of them at once and falls over again. The delay is a pure function of
    #: the attempt number, which is exactly what makes the herd synchronised.
    #:
    #: 0.5 by default: enough to spread a herd, while a delay is never less than half the intended
    #: one, so the backoff keeps the protective property it exists for. Zero disables it.
    jitter_fraction: float = 0.5

    #: Distinguishes deployments so two of them do not jitter identically.
    #:
    #: NOT A SECURITY BOUNDARY and not a secret. It exists so that two supervisors started from the
    #: same configuration at the same moment do not retry in lockstep -- if they shared a seed they
    #: would be as synchronised as with no jitter at all.
    jitter_seed: str = "tigerquant"

    def __post_init__(self) -> None:
        if self.initial_seconds <= 0:
            msg = "initial_seconds must be positive"
            raise ValueError(msg)
        if self.multiplier < 1:
            msg = "multiplier must be at least 1; a shrinking backoff is not a backoff"
            raise ValueError(msg)
        if self.max_seconds < self.initial_seconds:
            msg = "max_seconds must be at least initial_seconds"
            raise ValueError(msg)
        if self.max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)
        if not 0.0 <= self.jitter_fraction <= 1.0:
            msg = (
                "jitter_fraction must be between 0 and 1. A fraction above 1 would let a retry land "
                "BEFORE the previous one, and below 0 would make the delay longer than the ceiling."
            )
            raise ValueError(msg)

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based)."""
        if attempt < 1:
            msg = "attempt is 1-based"
            raise ValueError(msg)
        base = min(self.initial_seconds * (self.multiplier ** (attempt - 1)), self.max_seconds)
        if self.jitter_fraction == 0.0:
            return base
        # DERIVED, not random. `hashlib` rather than `random`, because a replay must reproduce the
        # delays it originally waited -- and a nondeterministic delay would make this path untestable.
        digest = hashlib.sha256(f"{self.jitter_seed}:{attempt}".encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
        # DOWNWARD ONLY, so a delay never exceeds the base and the ceiling keeps its meaning.
        return base * (1.0 - self.jitter_fraction * unit)


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
    #: How transient failures are retried. See BackoffPolicy.
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)

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
    #: True when the failure was classified transient and will be retried after a backoff.
    transient: bool = False

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
            "transient": self.transient,
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

    __slots__ = (
        "_alert_on_halt",
        "_config",
        "_cycle",
        "_is_transient",
        "_outcomes",
        "_router",
        "_stopped",
    )

    def __init__(
        self,
        cycle: Callable[[], bool | None],
        *,
        config: SupervisorConfig | None = None,
        router: AlertRouter | None = None,
        is_transient: Callable[[BaseException], bool] | None = None,
    ) -> None:
        """``cycle`` returns True when it detected a problem, False or None when clean.

        ``is_transient`` decides whether a failure is worth RETRYING. It defaults to never, so an
        unclassified failure halts the loop: retrying a bug for ever is the worse of the two
        mistakes, because a loop that keeps running looks like health.
        """
        self._cycle = cycle
        # Deny-by-default: an unclassified failure is treated as PERMANENT, so the loop halts
        # rather than retrying a bug for ever. Retrying a defect is the worse of the two
        # mistakes because it looks like health.
        self._is_transient = is_transient or (lambda _exc: False)
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
        transient_streak = 0
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
            raised: BaseException | None = None
            try:
                detected = bool(self._cycle())
                severity = "WARNING" if detected else ""
            except Exception as exc:  # noqa: BLE001 - a cycle must not kill the supervisor
                error = f"{type(exc).__name__}: {exc}"
                raised = exc

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

            transient, consecutive_failures, transient_streak, ok_cycles, failed_cycles = (
                self._classify(
                    error=error,
                    raised=raised,
                    consecutive_failures=consecutive_failures,
                    transient_streak=transient_streak,
                    ok_cycles=ok_cycles,
                    failed_cycles=failed_cycles,
                )
            )
            if transient:
                self._outcomes[-1] = replace(self._outcomes[-1], transient=True)
            if severity and severity > worst:
                worst = severity

            if transient and transient_streak < self._config.backoff.max_attempts:
                delay = self._config.backoff.delay_for(transient_streak)
                self._write_heartbeat(
                    index=index,
                    started_at=started,
                    phase="backoff",
                    last_ok=False,
                )
                self._sleep(delay)
                continue

            if transient and transient_streak >= self._config.backoff.max_attempts:
                reason = (
                    f"{TRANSIENT_EXHAUSTED}: {transient_streak} consecutive TRANSIENT failure(s) "
                    f"survived backoff up to {self._config.backoff.max_seconds}s; last error: "
                    f"{error}. Halting because a retry loop that outlives its incident is a "
                    "process nobody notices has stopped working."
                )
                self._halt(reason)
                return self._report(index, ok_cycles, failed_cycles, worst, halted_reason=reason)

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

            self._sleep(self._config.interval_seconds)

        return self._report(index, ok_cycles, failed_cycles, worst, halted_reason="")

    def _classify(
        self,
        *,
        error: str,
        raised: BaseException | None,
        consecutive_failures: int,
        transient_streak: int,
        ok_cycles: int,
        failed_cycles: int,
    ) -> tuple[bool, int, int, int, int]:
        """(transient, consecutive_failures, transient_streak, ok_cycles, failed_cycles).

        Two counters, deliberately. A flapping database and a broken code path are different
        incidents, and sharing one counter would let a transient blip spend the budget that exists
        to catch a real defect.

        The classifier is consulted only when a cycle actually raised, and it DEFAULTS TO NEVER,
        so an unclassified failure is permanent and halts.
        """
        if not error:
            # A cycle that RAN and found drift is a success. Counting it as a failure would halt
            # the supervisor for doing its job.
            return False, 0, 0, ok_cycles + 1, failed_cycles

        transient = raised is not None and self._is_transient(raised)
        if transient:
            return True, consecutive_failures, transient_streak + 1, ok_cycles, failed_cycles + 1
        return False, consecutive_failures + 1, transient_streak, ok_cycles, failed_cycles + 1

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices so a stop request is honoured promptly.

        A minute-long backoff should not make Ctrl-C wait a minute, and the same is true of the
        normal interval.
        """
        remaining = seconds
        while remaining > 0 and not self._stopped:
            nap = min(0.5, remaining)
            time.sleep(nap)
            remaining -= nap

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


#: What a heartbeat says about whether the process is MAKING PROGRESS.
#:
#: `HUNG` and `STALE` are different failures and need different responses: a hung process is alive and
#: holding resources, a stale one is gone. Collapsing them would lose the distinction that matters to
#: whoever has to respond at 3am.
class HangVerdict(StrEnum):
    """Whether the supervisor is progressing, and if not, how."""

    #: Between cycles, heartbeat fresh. Normal.
    IDLE = "IDLE"
    #: Waiting out a backoff, heartbeat fresh. Normal -- and NOT a hang, however long it looks.
    BACKOFF = "BACKOFF"
    #: Inside a cycle and within the bound. Normal.
    WORKING = "WORKING"
    #: Inside a cycle for longer than the bound. ALIVE BUT NOT PROGRESSING.
    HUNG = "HUNG"
    #: No heartbeat, or one too old to be about now. The process is gone or unreachable.
    STALE = "STALE"
    #: A heartbeat exists but cannot be interpreted -- a future version, or a partial write.
    UNKNOWN = "UNKNOWN"

    @property
    def is_healthy(self) -> bool:
        """IDLE, BACKOFF and WORKING are all healthy. HUNG and STALE are not."""
        return self in {HangVerdict.IDLE, HangVerdict.BACKOFF, HangVerdict.WORKING}


#: Phases that CAN hang. Everything else is expected to wait.
#:
#: `backoff` deliberately excluded: it exists to wait, and a long backoff is the mechanism working. A
#: detector that flagged it would fire on correct behaviour and would then be ignored.
_HANGABLE_PHASES = frozenset({"running"})


def detect_hang(
    heartbeat: dict[str, Any] | None,
    *,
    now: dt.datetime,
    hang_after_seconds: float,
) -> tuple[HangVerdict, str]:
    """Whether the heartbeat says the supervisor is progressing. Returns the verdict and why.

    ``hang_after_seconds`` bounds how long a SINGLE CYCLE may run before it is called hung. It is not
    a staleness bound: a process that is idle between cycles for longer than this is IDLE, not hung,
    and a process in backoff is BACKOFF.

    Returns a reason in every case, because "HUNG" alone does not tell a responder whether to restart
    the process or look at what it was waiting on.
    """
    if heartbeat is None:
        return HangVerdict.STALE, "no heartbeat: the process is gone or has never run"

    phase = str(heartbeat.get("phase", ""))
    written = heartbeat.get("written_at")
    cycle = heartbeat.get("cycle", "?")

    if not written:
        return HangVerdict.UNKNOWN, "the heartbeat carries no written_at, so its age is unknown"
    try:
        written_at = dt.datetime.fromisoformat(str(written))
    except ValueError:
        return HangVerdict.UNKNOWN, f"written_at {written!r} is not a timestamp"
    if written_at.tzinfo is None:
        return HangVerdict.UNKNOWN, "written_at has no timezone, so it cannot be compared to now"

    age = (ensure_utc(now) - ensure_utc(written_at)).total_seconds()

    if phase in _HANGABLE_PHASES:
        if age > hang_after_seconds:
            return (
                HangVerdict.HUNG,
                f"cycle {cycle} has been {phase} for {age:.0f}s, over the {hang_after_seconds:.0f}s "
                "bound. It is alive and not progressing -- blocked on something, or spinning.",
            )
        return HangVerdict.WORKING, f"cycle {cycle} is {phase}, {age:.0f}s in"

    if phase == "backoff":
        # Deliberately not a hang. Waiting is what backoff IS.
        return HangVerdict.BACKOFF, f"waiting out a backoff, last written {age:.0f}s ago"
    if phase == "idle":
        return HangVerdict.IDLE, f"between cycles, last written {age:.0f}s ago"
    return HangVerdict.UNKNOWN, f"unrecognised phase {phase!r}"


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
