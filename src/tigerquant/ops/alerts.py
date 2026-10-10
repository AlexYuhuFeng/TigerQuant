"""Alert routing: getting a suppression-worthy event in front of a human, once.

The drift monitor already decides *whether* something is wrong and knows about transition-based
alerting. What was missing is delivery, and the questions delivery raises are different ones:

* **An alert that is only counted is not an alert.** The monitor could report `alerted=True` and
  nothing left the process. This module is the thing that leaves.
* **The same condition must not page every cycle.** The monitor suppresses repeats; routing
  suppresses across SINKS and across sources, so a condition observed by two monitors is one page.
* **A suppressed alert must still be countable.** "Nothing happened" and "everything was
  suppressed" are different states, and a router that renders them identically is a router that
  hides an outage. Every suppression is counted and reported.
* **NO NETWORK BY DEFAULT.** The same posture as `HttpNewsConnector`, which refuses to fetch
  without an entitlement reference: a webhook sink refuses without an explicit endpoint, and
  nothing in this module opens a socket unless an operator configured one.

Delivery failure is deliberately non-fatal to trading but never silent: a sink that raises is
recorded as a failed delivery and the failure is reported, because an alerting path that fails
quietly is worse than one that does not exist.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tigerquant.core.clock import Clock, SystemClock
from tigerquant.core.enums import Severity
from tigerquant.core.errors import ConfigurationError
from tigerquant.core.redaction import redact

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "Alert",
    "AlertRouter",
    "AlertSink",
    "FileSink",
    "MemorySink",
    "RoutingReport",
    "SeverityFloor",
    "WebhookSink",
    "alert_from_drift",
    "severity_rank",
]

#: Ordering for comparisons. `Severity` is a StrEnum, so it has no inherent order of its own.
_SEVERITY_RANK: dict[str, int] = {
    Severity.INFO.value: 0,
    Severity.WARNING.value: 1,
    Severity.CRITICAL.value: 2,
}


def severity_rank(severity: Severity | str) -> int:
    """Rank a severity for comparison. An unknown severity ranks as CRITICAL.

    Deny-by-default, for the same reason `AccountIdentity.is_live_capable` is: an alert nobody
    can classify is one that must not be silently treated as routine.
    """
    return _SEVERITY_RANK.get(str(severity), _SEVERITY_RANK[Severity.CRITICAL.value])


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing an operator should know, independent of how it is delivered."""

    #: Stable identity of the CONDITION, not of the occurrence.
    #:
    #: Two alerts with the same key are the same problem, however many times it is observed. This
    #: is what makes suppression possible across sinks and across sources.
    key: str
    severity: Severity
    title: str
    detail: str
    raised_at: dt.datetime
    #: Where it came from: "drift", "watchdog", "risk". Free-form but used for grouping.
    source: str = "unknown"
    #: Machine-readable context. Sanitised before delivery.
    fields: dict[str, str] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        """A JSON-safe record with every free-text field sanitised."""
        return {
            "key": self.key,
            "severity": str(self.severity),
            "title": redact(self.title),
            "detail": redact(self.detail),
            "raised_at": self.raised_at.isoformat(),
            "source": self.source,
            "fields": {name: redact(value) for name, value in sorted(self.fields.items())},
        }

    def render(self) -> str:
        """One line for a log or a terminal."""
        record = self.as_record()
        detail = record["detail"]
        tail = f" -- {detail}" if detail else ""
        return f"[{record['severity']}] {record['source']}/{record['key']}: {record['title']}{tail}"


@dataclass(frozen=True, slots=True)
class SeverityFloor:
    """The minimum severity each sink wants, and how often it may be reminded.

    A floor rather than a filter because the question an operator asks is "what is worth waking me
    up", and the answer differs per channel.
    """

    minimum: Severity = Severity.WARNING
    #: Re-deliver an UNRESOLVED condition no more often than this. Zero means never repeat.
    reminder_interval_seconds: float = 0.0

    def admits(self, severity: Severity) -> bool:
        return severity_rank(severity) >= severity_rank(self.minimum)


@runtime_checkable
class AlertSink(Protocol):
    """Somewhere an alert can go."""

    @property
    def name(self) -> str: ...

    def deliver(self, alert: Alert) -> None: ...


class MemorySink:
    """Keeps alerts in a list. For tests, and for a dry run."""

    __slots__ = ("_alerts", "name")

    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self._alerts: list[Alert] = []

    @property
    def alerts(self) -> tuple[Alert, ...]:
        return tuple(self._alerts)

    def deliver(self, alert: Alert) -> None:
        self._alerts.append(alert)


class FileSink:
    """Appends one JSON object per line. The default, because it needs no network."""

    __slots__ = ("_path", "name")

    def __init__(self, path: Path, *, name: str = "file") -> None:
        self.name = name
        self._path = path

    def deliver(self, alert: Alert) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(alert.as_record(), sort_keys=True) + "\n")


class WebhookSink:
    """POSTs to a URL. Refuses to exist without one.

    Deliberately the only network path in this module, and deliberately awkward: it requires an
    explicit endpoint, and it will not fall back to a default host. A router that reached the
    network because a configuration field was empty is the failure this prevents.
    """

    __slots__ = ("_timeout", "_url", "name")

    def __init__(self, url: str, *, timeout_seconds: float = 10.0, name: str = "webhook") -> None:
        if not url or not url.startswith(("http://", "https://")):
            msg = (
                f"webhook sink {name!r} needs an explicit http(s) endpoint; got {url!r}. "
                "This module never contacts the network without one being configured."
            )
            raise ConfigurationError(msg)
        self.name = name
        self._url = url
        self._timeout = timeout_seconds

    def deliver(self, alert: Alert) -> None:
        import urllib.request

        body = json.dumps(alert.as_record(), sort_keys=True).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - the scheme is validated in __init__
            self._url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "TigerQuant-alerts"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            response.read()


@dataclass(frozen=True, slots=True)
class RoutingReport:
    """What happened to a batch of alerts. Suppressions are counted, never hidden."""

    considered: int = 0
    delivered: int = 0
    #: (alert key, sink name, reason) for every alert a sink did not receive.
    suppressed: tuple[tuple[str, str, str], ...] = ()
    #: (alert key, sink name, error) for every delivery that raised.
    failures: tuple[tuple[str, str, str], ...] = ()
    #: Distinct conditions seen, which is not the same as alerts delivered.
    distinct_conditions: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        parts = [
            f"alerts: {self.considered} considered, {self.delivered} delivered, "
            f"{len(self.suppressed)} suppressed, {len(self.failures)} failed"
        ]
        if self.failures:
            # Named, because a silently failing alerting path is worse than none.
            parts.append(f"FAILURES: {[f'{key}->{sink}' for key, sink, _ in self.failures]}")
        return "; ".join(parts)

    def as_record(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "delivered": self.delivered,
            "distinct_conditions": self.distinct_conditions,
            "suppressed": [list(item) for item in self.suppressed],
            "failures": [list(item) for item in self.failures],
            "ok": self.ok,
        }


class AlertRouter:
    """Decides which sink receives which alert, and refuses to send the same thing twice.

    The suppression rule is per `(key, sink)` and is a TRANSITION rule: the first occurrence of a
    condition is delivered, repeats are suppressed until either the reminder interval elapses or
    the condition clears and returns. That is the same shape the drift monitor uses, applied one
    level down, so a condition seen by two sources is still one page per sink.
    """

    __slots__ = ("_clock", "_last_delivered", "_sinks", "_suppressed_counts")

    def __init__(
        self,
        sinks: Sequence[tuple[AlertSink, SeverityFloor]],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._sinks = tuple(sinks)
        self._clock = clock or SystemClock()
        #: (key, sink name) -> when it was last delivered.
        self._last_delivered: dict[tuple[str, str], dt.datetime] = {}
        #: (key, sink name, reason) -> how many times it was suppressed. Counted so a quiet
        #: period can be told apart from a broken one.
        self._suppressed_counts: dict[tuple[str, str, str], int] = {}

    @property
    def sinks(self) -> tuple[AlertSink, ...]:
        return tuple(sink for sink, _ in self._sinks)

    def suppressed_counts(self) -> dict[str, int]:
        """How many times each condition was withheld, keyed 'sink/key:reason'.

        Exposed because a router that only reports deliveries cannot distinguish a quiet system
        from a broken one.
        """
        return {
            f"{sink}/{key}:{reason}": count
            for (key, sink, reason), count in sorted(self._suppressed_counts.items())
        }

    def clear(self, key: str) -> None:
        """Forget a condition, so its next occurrence counts as new.

        Called when a condition resolves. Without it, a problem that comes back after an hour
        would stay suppressed, which is the failure mode of every naive deduplicator.
        """
        for item in [item for item in self._last_delivered if item[0] == key]:
            del self._last_delivered[item]

    def route(self, alerts: Iterable[Alert]) -> RoutingReport:
        """Deliver every alert that should be delivered, and account for the rest."""
        considered = 0
        delivered = 0
        suppressed: list[tuple[str, str, str]] = []
        failures: list[tuple[str, str, str]] = []
        conditions: set[str] = set()

        for alert in alerts:
            considered += 1
            conditions.add(alert.key)
            for sink, floor in self._sinks:
                reason = self._suppression_reason(alert, sink, floor)
                if reason is not None:
                    suppressed.append((alert.key, sink.name, reason))
                    self._suppressed_counts[(alert.key, sink.name, reason)] = (
                        self._suppressed_counts.get((alert.key, sink.name, reason), 0) + 1
                    )
                    continue
                try:
                    sink.deliver(alert)
                except Exception as exc:  # noqa: BLE001 - one bad sink must not stop the rest
                    failures.append((alert.key, sink.name, f"{type(exc).__name__}: {exc}"))
                    continue
                delivered += 1
                self._last_delivered[(alert.key, sink.name)] = self._clock.now()

        return RoutingReport(
            considered=considered,
            delivered=delivered,
            suppressed=tuple(suppressed),
            failures=tuple(failures),
            distinct_conditions=len(conditions),
        )

    def _suppression_reason(
        self,
        alert: Alert,
        sink: AlertSink,
        floor: SeverityFloor,
    ) -> str | None:
        """Why this sink should NOT receive this alert, or None to deliver it."""
        if not floor.admits(alert.severity):
            return "below-severity-floor"
        last = self._last_delivered.get((alert.key, sink.name))
        if last is None:
            return None
        if floor.reminder_interval_seconds <= 0:
            return "already-delivered"
        elapsed = (self._clock.now() - last).total_seconds()
        if elapsed < floor.reminder_interval_seconds:
            return "within-reminder-interval"
        return None


def alert_from_drift(result: Any, *, source: str = "drift") -> Alert | None:
    """Convert a `DriftCheckResult` into an Alert, or None when nothing is wrong.

    Returns None rather than an INFO alert for a consistent check, because an alert stream that
    contains routine successes is one an operator learns to ignore.
    """
    if getattr(result, "consistent", False):
        return None
    if not getattr(result, "alerted", False):
        # The monitor decided this is a repeat of a condition it already reported. Silence here is
        # correct: the router does its own suppression, and double-suppressing loses the count.
        return None

    severity = getattr(result, "severity", Severity.WARNING)
    detail = str(getattr(result, "detail", "") or "")
    return Alert(
        key="drift:journal-database",
        severity=severity,
        title="journal and database disagree",
        detail=detail,
        raised_at=result.checked_at,
        source=source,
        fields={
            "occurrences": str(getattr(result, "occurrences", "")),
            "escalated": str(getattr(result, "escalated", "")),
        },
    )
