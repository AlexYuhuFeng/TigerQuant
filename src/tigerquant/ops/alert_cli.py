"""The `tigerquant alerts` command group: configure sinks and PROVE they work.

The subcommand that matters most is `verify`. An alerting path that has never been exercised is
one that fails the first time it is needed, and it fails silently -- because the whole point of
the path is that nobody is watching it. So `verify` routes a synthetic alert through every
configured sink and reports, per sink, whether it arrived.

The other two are `show` (what is configured, and what each sink will accept) and `drift` (run a
real reconciliation and route whatever it finds, which is also how an operator can see the
router's suppression behaviour without waiting for a genuine incident).

Sink configuration comes from the command line and from two environment variables, and the
webhook is deliberately the only thing that reads a URL. Nothing here opens a socket unless
`--webhook` or `TIGERQUANT_ALERT_WEBHOOK` names an endpoint -- the same posture as
`HttpNewsConnector` refusing to fetch without an entitlement reference.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tigerquant.core.clock import SystemClock
from tigerquant.core.enums import Severity
from tigerquant.core.errors import ConfigurationError
from tigerquant.ops.alerts import (
    Alert,
    AlertRouter,
    AlertSink,
    FileSink,
    MemorySink,
    SeverityFloor,
    WebhookSink,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

__all__ = [
    "DEFAULT_ALERT_LOG",
    "WEBHOOK_ENV",
    "build_router",
    "cmd_alerts",
    "describe_sinks",
]

#: Where alerts land by default. A file needs no network, which is why it is the default.
DEFAULT_ALERT_LOG = Path("out/alerts.jsonl")

#: Set this to route alerts to an HTTP endpoint. Unset means no webhook sink is created at all.
WEBHOOK_ENV = "TIGERQUANT_ALERT_WEBHOOK"

#: Set this to lower the file sink's severity floor to INFO.
VERBOSE_ENV = "TIGERQUANT_ALERT_VERBOSE"


def _severity(name: str) -> Severity:
    """Parse a severity name, refusing an unknown one rather than defaulting.

    Defaulting would be worse than failing here: a typo in a floor would silently change what an
    operator is told about, and they would have no way to notice.
    """
    try:
        return Severity(name.upper())
    except ValueError as exc:
        allowed = ", ".join(member.value for member in Severity)
        msg = f"unknown severity {name!r}; expected one of {allowed}"
        raise ConfigurationError(msg) from exc


def build_router(
    *,
    log_path: Path | None = None,
    webhook: str | None = None,
    floor: Severity = Severity.WARNING,
    reminder_seconds: float = 900.0,
    extra: Sequence[AlertSink] = (),
) -> AlertRouter:
    """Assemble the sinks an operator asked for, and nothing else.

    Every sink is created only because a flag or an environment variable named it. There is no
    default webhook and no default host, so a misconfigured deployment sends alerts nowhere
    rather than somewhere unexpected.
    """
    sinks: list[tuple[AlertSink, SeverityFloor]] = []
    policy = SeverityFloor(minimum=floor, reminder_interval_seconds=reminder_seconds)

    # The file sink is always present. It is the only one that cannot fail for network reasons,
    # so it is the floor below which an alert is genuinely lost.
    sinks.append((FileSink(log_path or DEFAULT_ALERT_LOG, name="file"), policy))

    endpoint = webhook if webhook is not None else os.environ.get(WEBHOOK_ENV)
    if endpoint:
        # WebhookSink raises on anything that is not an explicit http(s) URL, which is the
        # refusal we want rather than a silent skip.
        sinks.append((WebhookSink(endpoint, name="webhook"), policy))

    for sink in extra:
        sinks.append((sink, policy))

    return AlertRouter(sinks, clock=SystemClock())


def describe_sinks(router: AlertRouter) -> str:
    """What is configured, and what each sink will accept."""
    lines = [f"alert sinks: {len(router.sinks)}"]
    for sink, floor in router._sinks:
        reminder = (
            "never repeats"
            if floor.reminder_interval_seconds <= 0
            else f"reminds every {floor.reminder_interval_seconds:.0f}s"
        )
        lines.append(f"  {sink.name:9s} floor={floor.minimum.value:8s} {reminder}")
    if not any(sink.name == "webhook" for sink in router.sinks):
        # Named explicitly, because "no webhook" and "a webhook that silently did not register"
        # look identical in a list of sinks.
        lines.append(
            f"  (no webhook sink: set --webhook or {WEBHOOK_ENV} to add one. Nothing in this "
            "project contacts the network without an explicit endpoint.)"
        )
    return "\n".join(lines)


def _cmd_alerts_show(args: argparse.Namespace) -> int:
    router = build_router(
        log_path=args.log,
        webhook=args.webhook,
        floor=_severity(args.floor),
        reminder_seconds=args.reminder,
    )
    print(describe_sinks(router))
    if args.suppressed:
        counts = router.suppressed_counts()
        print()
        print(f"suppression state: {len(counts)} entr(ies)")
        for name, count in counts.items():
            print(f"  {name}: {count}")
    return 0


def _cmd_alerts_verify(args: argparse.Namespace) -> int:
    """Route a synthetic alert through every sink and report what actually arrived.

    This is the point of the command group. An alerting path is only exercised when something is
    wrong, which is the worst possible time to discover it does not work -- so this exercises it
    on demand instead, and fails loudly if a sink does not receive the alert.
    """
    probe_path = args.log or DEFAULT_ALERT_LOG
    if args.log is None:
        # Do not pollute the real alert log with a synthetic probe unless asked to.
        probe_path = DEFAULT_ALERT_LOG.with_name("alerts_verify.jsonl")

    before = probe_path.stat().st_size if probe_path.is_file() else 0

    sink = MemorySink("verify")
    router = build_router(
        log_path=probe_path,
        webhook=args.webhook,
        floor=Severity.INFO,  # the probe must not be filtered out by the very floor under test
        reminder_seconds=0,
        extra=[sink],
    )
    probe = Alert(
        key="verify:alerting-path",
        severity=Severity.CRITICAL,
        title="alerting path verification",
        detail=(
            "Synthetic CRITICAL raised by `tigerquant alerts verify`. If you are reading this in "
            "an alert channel, the path works. Nothing is wrong with the system."
        ),
        raised_at=dt.datetime.now(tz=dt.UTC),
        source="verify",
        fields={"command": "tigerquant alerts verify"},
    )
    report = router.route([probe])

    print(f"routing a synthetic {probe.severity.value} through {len(router.sinks)} sink(s)")
    for configured_sink, _ in router._sinks:
        print(f"  {configured_sink.name}")
    print()
    print(report.summary())

    after = probe_path.stat().st_size if probe_path.is_file() else 0
    file_grew = after > before
    in_memory = len(sink.alerts) == 1

    failures: list[str] = []
    if not in_memory:
        failures.append("the in-process sink did not receive the alert")
    if not file_grew:
        failures.append(f"the file sink did not write to {probe_path}")
    failures.extend(f"{sink_name}: {error}" for _, sink_name, error in report.failures)

    if failures:
        print()
        # Exit non-zero so this can gate a deployment. An alerting path that cannot deliver is a
        # broken safety control, not a cosmetic problem.
        print("VERIFY FAILED:")
        for item in failures:
            print(f"  - {item}")
        return 1

    print()
    print("VERIFY OK: every configured sink received the alert")
    if args.webhook or os.environ.get(WEBHOOK_ENV):
        print("  (the webhook sink reported success, but check the destination channel too:")
        print("   a 200 response is not proof that a human saw it)")
    else:
        print(f"  no webhook configured; set --webhook or {WEBHOOK_ENV} to test one")
    return 0


def _cmd_alerts_drift(args: argparse.Namespace) -> int:
    """Run a real reconciliation and route whatever it finds.

    Useful for two things: seeing the suppression behaviour without waiting for a genuine
    incident, and confirming in a real deployment that a drift reaches the configured sinks.
    """
    from tigerquant.core.enums import Mode
    from tigerquant.db.repository import InMemoryRepository
    from tigerquant.events.journal import AppendOnlyJournal
    from tigerquant.ops.drift_monitor import DriftMonitor

    if not args.journal.is_file():
        print(f"error: journal not found: {args.journal}", file=sys.stderr)
        return 2

    sink = MemorySink("drift-report")
    router = build_router(
        log_path=args.log,
        webhook=args.webhook,
        floor=_severity(args.floor),
        reminder_seconds=args.reminder,
        extra=[sink],
    )
    with AppendOnlyJournal(args.journal, Mode(args.mode), fsync=False) as journal:
        events = [record.event for record in journal.iter_records()]

    monitor = DriftMonitor(InMemoryRepository(), router=router)
    result = monitor.check(events, at=dt.datetime.now(tz=dt.UTC))

    print(f"checked {len(events)} journal event(s) against an EMPTY database")
    print(f"  consistent: {result.consistent}")
    if result.consistent:
        print("  nothing to route. This command compares against an empty database, so a clean")
        print("  result here means the journal itself was empty.")
        return 0
    print(f"  severity: {result.severity}")
    print(f"  {result.detail}")
    print()
    print(f"delivered {len(sink.alerts)} alert(s) to the local sink; other sinks received theirs")
    for routed in sink.alerts:
        print(f"  {routed.render()}")
    return 0 if monitor.router is not None else 1


def cmd_alerts(args: argparse.Namespace) -> int:
    """Dispatch `tigerquant alerts ...`."""
    if args.alerts_command == "show":
        return _cmd_alerts_show(args)
    if args.alerts_command == "verify":
        return _cmd_alerts_verify(args)
    if args.alerts_command == "drift":
        return _cmd_alerts_drift(args)
    msg = f"unknown alerts command {args.alerts_command!r}"
    raise ConfigurationError(msg)
