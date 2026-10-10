"""``tigerquant`` command line.

There is deliberately no command that enables live trading, changes a risk limit, or
touches a brokerage account. The CLI exposes three safe operations:

* ``replay`` — run the offline vertical slice against a scenario fixture;
* ``verify-journal`` — recompute the audit-journal hash chain;
* ``doctor`` — report the environment, the mode matrix and the safety self-checks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant import IMPLEMENTATION_STATUS, __version__
from tigerquant.core.clock import SystemClock
from tigerquant.core.enums import Mode, Severity, capabilities_for
from tigerquant.core.errors import ConfigurationError, IntegrationUnavailable, TigerQuantError
from tigerquant.db.postgres import PostgresRepository, connect
from tigerquant.db.projector import check_drift, project
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.events.registry import event_type_names
from tigerquant.market.fixtures import load_scenario
from tigerquant.ops.alert_cli import cmd_alerts
from tigerquant.ops.dashboard import DashboardServer, build_app
from tigerquant.ops.drift_monitor import DriftMonitor
from tigerquant.ops.lineage_report import build_lineage, export
from tigerquant.ops.metrics_report import attribution_by_decision, measure_session
from tigerquant.ops.settings import (
    CREDENTIALS,
    clear_credential,
    known_credentials,
    mark_credential_verified,
    set_secret,
    settings_report,
)
from tigerquant.pipeline.replay import run_replay

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_parser", "main"]


def _add_settings_parsers(sub: Any) -> None:
    """Register the settings command group.

    Extracted because ``build_parser`` had grown past the complexity limit, and a parser that
    is one long function is hard to review: the whole point of a command registry is that each
    command's surface is visible on its own.
    """
    settings = sub.add_parser(
        "settings",
        help="show, set, verify or clear Tiger and model-provider credentials",
    )
    settings_sub = settings.add_subparsers(dest="settings_command", required=True)

    show = settings_sub.add_parser("show", help="report every credential's state, masked")
    show.add_argument("--json", action="store_true", help="emit JSON instead of text")

    setter = settings_sub.add_parser(
        "set",
        help="store a credential in the secret store (reads the secret from stdin)",
    )
    setter.add_argument("key", choices=known_credentials())
    setter.add_argument(
        "--value",
        default=None,
        help=(
            "value for a NON-SECRET setting. For a secret, omit this and pipe it on stdin so "
            "it does not reach shell history or the process list."
        ),
    )

    verify = settings_sub.add_parser("verify", help="mark a credential as exercised successfully")
    verify.add_argument("key", choices=known_credentials())

    clear = settings_sub.add_parser("clear", help="remove a credential and its marker")
    clear.add_argument("key", choices=known_credentials())


def _add_alerts_parsers(sub: Any) -> None:
    """Register the `alerts` command group.

    `verify` is the one that earns its place: an alerting path is only ever exercised when
    something is wrong, which is the worst moment to find out it does not work.
    """
    alerts = sub.add_parser(
        "alerts",
        help="inspect alert sinks, PROVE they receive alerts, and route a real drift check",
    )
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)

    show = alerts_sub.add_parser("show", help="report the configured sinks and their floors")
    show.add_argument("--suppressed", action="store_true", help="also report suppression counts")

    verify = alerts_sub.add_parser(
        "verify",
        help=(
            "route a synthetic CRITICAL through every sink and report what actually arrived. "
            "Exits non-zero if any sink failed, so it can gate a deployment."
        ),
    )
    verify.add_argument(
        "--log",
        type=Path,
        default=None,
        help=(
            "file sink for the probe. Defaults to out/alerts_verify.jsonl so a probe never "
            "pollutes the real alert log."
        ),
    )

    drift = alerts_sub.add_parser(
        "drift",
        help="check a journal against an empty database and route what it finds",
    )
    drift.add_argument("journal", type=Path, help="audit journal JSONL to reconcile")
    drift.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="the mode this journal belongs to",
    )

    show.add_argument(
        "--log",
        type=Path,
        default=None,
        help="file sink path (default: out/alerts.jsonl)",
    )

    drift.add_argument(
        "--log",
        type=Path,
        default=None,
        help="file sink path (default: out/alerts.jsonl)",
    )

    # Shared by all three: which OTHER sinks exist. `--log` is declared per subcommand above
    # because `verify` defaults it somewhere different, and a duplicate declaration would be an
    # argparse error rather than an override.
    for parser in (show, verify, drift):
        parser.add_argument(
            "--webhook",
            default=None,
            help=(
                "http(s) endpoint for a webhook sink. Omit and nothing contacts the network; "
                "TIGERQUANT_ALERT_WEBHOOK is the environment equivalent."
            ),
        )
        parser.add_argument(
            "--floor",
            default=Severity.WARNING.value,
            help="minimum severity the sinks accept (INFO, WARNING, CRITICAL)",
        )
        parser.add_argument(
            "--reminder",
            type=float,
            default=900.0,
            help="seconds before an UNRESOLVED condition is delivered again; 0 never repeats",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tigerquant",
        description=(
            "TigerQuant — evidence-grounded, paper-first AI trading platform. "
            "Live trading is not implemented and cannot be enabled here."
        ),
    )
    parser.add_argument("--version", action="version", version=f"tigerquant {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="run the offline synthetic vertical slice")
    replay.add_argument("--fixture", required=True, type=Path, help="scenario fixture YAML")
    replay.add_argument("--journal", type=Path, default=None, help="write the audit journal here")
    replay.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    replay.add_argument("--quiet", action="store_true", help="only print the journal digest")

    verify = sub.add_parser("verify-journal", help="recompute the audit-journal hash chain")
    verify.add_argument("journal", type=Path)
    verify.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="expected journal mode",
    )

    sub.add_parser("doctor", help="report environment, mode matrix and safety checks")

    project = sub.add_parser(
        "project",
        help="project a journal into PostgreSQL, then report journal/database drift",
    )
    project.add_argument("journal", type=Path, help="audit journal JSONL to project")
    project.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="mode whose database to project into (ADR-0013: one database per mode)",
    )
    project.add_argument(
        "--dsn",
        default=None,
        help="database to project into; defaults to the mode's configured DSN",
    )
    project.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    project.add_argument(
        "--allow-gaps",
        action="store_true",
        help="project even though the journal has a sequence gap (not recommended)",
    )

    drift = sub.add_parser(
        "drift",
        help="check the journal against the database, once or on a cadence",
    )
    drift.add_argument("journal", type=Path, help="audit journal JSONL to compare")
    drift.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="mode whose database to compare against",
    )
    drift.add_argument("--dsn", default=None, help="database to compare; defaults per mode")
    drift.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help="0 runs a single check; a positive value runs that cadence until interrupted",
    )
    drift.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="how many checks to run when --interval-seconds is positive",
    )
    drift.add_argument("--report", type=Path, default=None, help="write the JSON report here")

    report = sub.add_parser(
        "report",
        help="render decision/evidence/execution lineage from a journal",
    )
    report.add_argument("journal", type=Path, help="audit journal JSONL to read")
    report.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="the mode this journal belongs to; it is stamped on the output",
    )
    report.add_argument(
        "--format",
        default="text",
        choices=("text", "json", "csv"),
        help="output format",
    )
    report.add_argument("--out", type=Path, default=None, help="write here instead of stdout")

    dash = sub.add_parser(
        "dashboard",
        help="serve the lineage on loopback, read-only (development inspection tool)",
    )
    dash.add_argument("journal", type=Path, help="audit journal JSONL to display")
    dash.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="the mode this journal belongs to",
    )
    dash.add_argument("--host", default="127.0.0.1", help="loopback address only")
    dash.add_argument("--port", type=int, default=8787, help="port to bind")

    metrics = sub.add_parser(
        "metrics",
        help="measure a session: P&L attribution, step durations, activity counts",
    )
    metrics.add_argument("journal", type=Path, help="audit journal JSONL to measure")
    metrics.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="the mode this journal belongs to",
    )
    metrics.add_argument(
        "--clock-source",
        default="SimulatedClock",
        help=(
            "which clock produced the timestamps. The default is honest for an offline "
            "replay; pass SystemClock only for a real session, or the durations will be "
            "reported as step sizes rather than latency."
        ),
    )
    metrics.add_argument(
        "--by-decision",
        action="store_true",
        help="also list each decision's contribution",
    )
    metrics.add_argument("--report", type=Path, default=None, help="write the JSON report here")

    _add_settings_parsers(sub)
    _add_alerts_parsers(sub)
    return parser


def _cmd_replay(args: argparse.Namespace) -> int:
    fixture = load_scenario(args.fixture)
    report = run_replay(fixture, journal_path=args.journal)
    payload = report.model_dump(mode="json")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    if args.quiet:
        print(f"{report.journal_digest}")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    mode = Mode(args.mode)
    journal = AppendOnlyJournal(args.journal, mode)
    verification = journal.verify()
    if not verification.ok:
        print(
            f"INVALID: {verification.first_error} at seq {verification.first_error_seq} "
            f"after {verification.records} record(s)",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {verification.records} record(s), head {verification.digest}")
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    """Compare the journal with the database and report.

    Exit codes: 0 consistent, 1 drifted, 2 the database could not be reached. A drift is
    reported and never repaired — we do not know which record is wrong, so overwriting either
    would destroy the evidence needed to find out (ADR-0004 s5).
    """
    mode = Mode(args.mode)
    dsn = args.dsn or os.environ.get("TIGERQUANT_DB_DSN") or _dev_dsn(mode)
    try:
        connection = connect(dsn)
    except IntegrationUnavailable as exc:
        print(f"database unavailable: {exc}", file=sys.stderr)
        return 2
    except TigerQuantError as exc:
        print(f"cannot reach the database: {exc}", file=sys.stderr)
        return 2

    journal_file = Path(args.journal)
    with connection:
        repository = PostgresRepository(connection, mode=mode)
        clock = SystemClock()
        # No emitter: a drift inspection reads. Emitting an alert into the journal here
        # would append an event the database does not yet have, and the NEXT check would
        # then report drift caused by the monitor itself -- a false positive.
        monitor = DriftMonitor(repository=repository)
        results = []
        iterations = max(args.iterations, 1) if args.interval_seconds > 0 else 1
        for index in range(iterations):
            with AppendOnlyJournal(journal_file, mode, fsync=False) as journal:
                events = tuple(record.event for record in journal.iter_records())
            results.append(monitor.check(events, at=clock.now()))
            if index < iterations - 1:
                print(f"check {index + 1}: {results[-1].describe()}")
                time.sleep(args.interval_seconds)

    for result in results:
        print(result.describe())

    payload = {
        "mode": mode.value,
        "checks": [result.as_record() for result in results],
        "alert_rate": monitor.alert_rate(),
        "blocks_new_exposure": any(r.blocks_new_exposure for r in results),
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    return 0 if results[-1].consistent else 1


def _cmd_report(args: argparse.Namespace) -> int:
    """Render lineage for one journal. Read-only: this command writes nothing but its output."""
    mode = Mode(args.mode)
    path = Path(args.journal)
    with AppendOnlyJournal(path, mode, fsync=False) as journal:
        events = tuple(record.event for record in journal.iter_records())

    report = build_lineage(events, mode=mode.value)
    rendered = export(report, args.format)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.out} ({report.summary()})")
    else:
        print(rendered)

    # A non-zero exit for a report that needs attention makes this usable as a gate.
    return 0 if report.is_clean else 1


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the lineage read-only on loopback.

    Binds only a loopback address: there is no authentication and no TLS, so anything else
    would be an exposed endpoint. Prints the URL and blocks until interrupted.
    """
    mode = Mode(args.mode)
    path = Path(args.journal)
    with AppendOnlyJournal(path, mode, fsync=False) as journal:
        events = tuple(record.event for record in journal.iter_records())

    report = build_lineage(events, mode=mode.value)
    app = build_app(report, journal=str(path))
    try:
        server = DashboardServer(app, host=args.host, port=args.port)
    except ConfigurationError as exc:
        print(f"refusing to serve: {exc}", file=sys.stderr)
        return 2

    print(f"mode {mode.value}: {report.summary()}")
    print(f"serving read-only on {server.base_url} (loopback only; no auth, no TLS)")
    print("Ctrl-C to stop.")
    try:
        server.start()
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()
    return 0


def _cmd_metrics(args: argparse.Namespace) -> int:
    """Measure one journal. Read-only; writes nothing but its report.

    Exit codes: 0 when the P&L attribution reconciles with the ledger, 1 when it does not.
    A report whose parts do not add up should be rejected, not published quietly.
    """
    mode = Mode(args.mode)
    path = Path(args.journal)
    with AppendOnlyJournal(path, mode, fsync=False) as journal:
        events = tuple(record.event for record in journal.iter_records())

    session = measure_session(events, mode=mode.value, clock_source=args.clock_source)
    payload: dict[str, object] = session.as_record()
    if args.by_decision:
        payload["by_decision"] = list(attribution_by_decision(events, mode=mode.value))

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.report}")

    print(session.summary())
    print(f"  {session.latency.summary()}")
    for key, value in sorted(session.facts.items()):
        print(f"  {key}: {value}")

    return 0 if session.attribution.reconciles() else 1


def _cmd_settings(args: argparse.Namespace) -> int:
    """Show, set, verify or clear credentials.

    The store is checked against Git before anything is written: a settings command that could
    put a key into a public repository is worse than no settings command.
    """
    command = args.settings_command

    if command == "show":
        report = settings_report()
        if args.json:
            print(json.dumps(report.as_record(), indent=2, sort_keys=True))
        else:
            print(report.render())
        # Exit 1 when the credentials a paper session needs are absent, so this can gate a run.
        return 0 if report.ready_for_paper else 1

    if command == "set":
        spec = next(item for item in CREDENTIALS if item.key == args.key)
        if args.value is not None:
            if spec.secret:
                print(
                    f"refusing --value for the secret {args.key!r}: it would appear in shell "
                    "history and in the process list. Pipe it on stdin instead:\n"
                    f"  echo <value> | python -m tigerquant settings set {args.key}",
                    file=sys.stderr,
                )
                return 2
            value = args.value
        else:
            value = sys.stdin.read()
        try:
            written = set_secret(args.key, value)
        except ConfigurationError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(f"stored {args.key} at {written}")
        return 0

    if command == "verify":
        mark_credential_verified(args.key)
        print(f"marked {args.key} verified")
        return 0

    # clear
    removed = clear_credential(args.key)
    print(
        f"removed {len(removed)} file(s) for {args.key}" if removed else f"{args.key} was not set"
    )
    return 0


def _credential_specs() -> tuple[object, ...]:

    return CREDENTIALS


def _dev_dsn(mode: Mode) -> str:
    """The loopback development DSN for a mode.

    One database and one role per mode (ADR-0013 s2), and the server refuses cross-mode
    CONNECT, so a wrong mode fails loudly rather than writing into the wrong history.
    Development credentials only; a deployment supplies --dsn or TIGERQUANT_DB_DSN.
    """
    role = f"tq_{mode.value.lower()}"
    return f"postgresql://{role}:tq_local_development_only@127.0.0.1:5432/{role}"


def _describe_dsn(dsn: str) -> str:
    """Render a DSN without its password, for reporting."""
    head, _, tail = dsn.rpartition("@")
    user = head.rpartition("//")[2].partition(":")[0]
    return f"{user}@{tail}" if tail else "(unparseable DSN)"


def _cmd_project(args: argparse.Namespace) -> int:
    """Project a journal into the mode's database, then report drift.

    Exit codes are meaningful: 0 means the projection was applied AND the journal and the
    database agree, 1 means they do not. A drift is never repaired here — the caller
    decides, because we do not know which of the two records is wrong.
    """
    mode = Mode(args.mode)
    dsn = args.dsn or os.environ.get("TIGERQUANT_DB_DSN") or _dev_dsn(mode)
    try:
        connection = connect(dsn)
    except IntegrationUnavailable as exc:
        print(f"database unavailable: {exc}", file=sys.stderr)
        return 2
    except TigerQuantError as exc:
        print(f"cannot reach the database: {exc}", file=sys.stderr)
        return 2

    with connection:
        repository = PostgresRepository(connection, mode=mode)
        journal_file = Path(args.journal)
        # Opening through AppendOnlyJournal verifies the hash chain on the way in: a corrupt
        # journal must not become a confidently wrong projection.
        with AppendOnlyJournal(journal_file, mode, fsync=False) as journal:
            events = tuple(record.event for record in journal.iter_records())
        report = project(events, repository, require_contiguous_sequence=not args.allow_gaps)
        drift = check_drift(events, repository)

    payload = {
        "mode": mode.value,
        "projection": {
            "events_seen": report.events_seen,
            "events_applied": report.events_applied,
            "events_already_present": report.events_already_present,
            "order_rows": report.order_rows,
            "orders_tracked": report.orders_tracked,
            "orders_open": report.orders_open,
            "portfolio_marks": report.portfolio_marks,
            "last_sequence": report.last_sequence,
            "digest": report.digest(),
        },
        "drift": drift.as_record(),
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    print(f"mode {mode.value}: {report.describe()}")
    print(f"  {drift.summary()}")
    return 0 if drift.is_consistent else 1


def _cmd_doctor(_: argparse.Namespace) -> int:
    print(f"tigerquant {__version__} — implementation status: {IMPLEMENTATION_STATUS}")
    print("modes:")
    for mode in Mode:
        caps = capabilities_for(mode)
        authorized = capabilities_for(mode, live_authorized=True)
        print(
            f"  {mode.value:9s} namespace={caps.namespace} "
            f"submit={caps.can_submit_orders} cancel={caps.can_cancel_orders} "
            f"live_authorized_submit={authorized.can_submit_orders} "
            f"requires_authorization={caps.requires_authorization}"
        )
    print(f"registered event types: {len(event_type_names())}")
    print("safety checks:")
    checks = _safety_checks()
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def _safety_checks() -> list[tuple[str, bool, str]]:
    """Cheap, honest self-checks. These are diagnostics, not the test suite.

    The checks parse the source with :mod:`ast` rather than grepping it, so a mention of
    a banned pattern inside a docstring or a string constant is not a false positive.
    """
    root = Path(__file__).resolve().parents[2]
    package_root = root / "src" / "tigerquant"
    source_files = sorted(package_root.rglob("*.py"))

    determinism_offenders = _determinism_offenders(source_files)
    live_offenders = _live_enablement_offenders(source_files)

    caps_ok = True
    for mode in Mode:
        caps = capabilities_for(mode)
        if mode in {Mode.BACKTEST, Mode.SHADOW} and caps.can_submit_orders:
            caps_ok = False
        if mode is Mode.LIVE and (
            capabilities_for(mode).can_submit_orders
            or not capabilities_for(mode).requires_authorization
        ):
            caps_ok = False

    return [
        (
            "determinism (no uuid4 / wall-clock calls outside core.clock)",
            not determinism_offenders,
            "clean"
            if not determinism_offenders
            else f"offenders: {', '.join(determinism_offenders)}",
        ),
        (
            "mode capabilities (no submission in BACKTEST/SHADOW, LIVE gated)",
            caps_ok,
            "clean" if caps_ok else "mode matrix is inconsistent",
        ),
        (
            "no live enablement surface in source",
            not live_offenders,
            "clean" if not live_offenders else f"suspicious: {', '.join(live_offenders)}",
        ),
        (
            "implementation status is labelled",
            IMPLEMENTATION_STATUS in {"SIMULATED", "PAPER_VERIFIED", "UNAVAILABLE"},
            IMPLEMENTATION_STATUS,
        ),
    ]


#: Modules allowed to read the wall clock, because they *are* the clock abstraction.
_CLOCK_MODULES = {"clock.py"}


#: Files allowed to read the wall clock, each for a reason that is NOT reproducibility.
#:
#: The rule exists because a wall-clock read inside replayable logic breaks byte-identical
#: replay. These reads are outside it: they timestamp an operator-facing artifact or measure a
#: real elapsed interval, and none of them contributes to a journaled event.
#:
#: Named explicitly rather than pattern-matched, so adding one is a deliberate act. Closing an
#: aliasing hole in this very check surfaced all four at once, which is the argument for keeping
#: the list short and reviewed.
_OPERATIONAL_CLOCK_MODULES = frozenset(
    {
        # Timestamps a verification marker: "when did we last check this credential".
        "settings.py",
        # A CLI command a HUMAN runs on demand. "Now" is the correct instant for a synthetic
        # probe and for a reconciliation the operator asked for at this moment; neither is
        # replayed, and neither contributes a journaled event. This module was caught by the
        # determinism check the moment it was added, which is the control working.
        "alert_cli.py",
        # The provider's fallback clock when the composition root supplies none. A model call is
        # not replayed; the prompt digest is what proves what was sent.
        "model.py",
    }
)


def _determinism_offenders(source_files: list[Path]) -> list[str]:
    """Find non-deterministic calls that would break byte-identical replay."""
    import ast

    banned_calls = {"uuid4", "uuid1", "monotonic", "perf_counter"}
    banned_dotted = {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("time", "time"),
        ("uuid", "uuid4"),
    }
    offender_names: list[str] = []
    for path in source_files:
        if path.name in _CLOCK_MODULES or path.name in _OPERATIONAL_CLOCK_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Aliases matter. `import datetime as dt` makes the call `dt.datetime.now(...)`, whose
        # receiver is an Attribute rather than a Name, so a detector testing only for
        # `ast.Name` silently misses it -- which is exactly how the guard's own staleness check
        # slipped past this control. Resolve each module alias to its real name first.
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        def resolved(expr: ast.AST, table: dict[str, str] = aliases) -> str | None:
            """The dotted path an expression refers to, following import aliases.

            ``table`` is bound as a default rather than captured from the enclosing loop. A
            closure over a loop variable is the classic late-binding bug — ruff's B023 — and
            here it would silently resolve every file against the LAST file's aliases, so the
            check would pass or fail depending on alphabetical order.
            """
            parts: list[str] = []
            cursor: ast.AST | None = expr
            while isinstance(cursor, ast.Attribute):
                parts.append(cursor.attr)
                cursor = cursor.value
            if not isinstance(cursor, ast.Name):
                return None
            head = table.get(cursor.id, cursor.id)
            return ".".join([head, *reversed(parts)])

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in banned_calls:
                offender_names.append(f"{path.name}:{func.id}()")
                continue
            path_name = resolved(func)
            if path_name is None:
                continue
            head, _, tail = path_name.rpartition(".")
            if (head.split(".")[-1], tail) in banned_dotted:
                offender_names.append(f"{path.name}:{path_name}()")
    return offender_names


def _live_enablement_offenders(source_files: list[Path]) -> list[str]:
    """Find code that would enable live trading. Absence is the expected result."""
    import ast

    guarded_names = {"allow_live", "live_deployment_authorized", "model_has_broker_write_access"}
    offenders: list[str] = []
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else ""
                )
                if name in {"enable_live", "activate_live", "go_live"}:
                    offenders.append(f"{path.name}:{name}()")
            targets: list[str] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                value = node.value
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                value = node.value
                targets = [node.target.id]
            if not targets or not isinstance(value, ast.Constant) or value.value is not True:
                continue
            for target in targets:
                if target in guarded_names:
                    offenders.append(f"{path.name}:{target} = True")
    return offenders


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "replay": _cmd_replay,
        "project": _cmd_project,
        "drift": _cmd_drift,
        "report": _cmd_report,
        "dashboard": _cmd_dashboard,
        "metrics": _cmd_metrics,
        "settings": _cmd_settings,
        "alerts": cmd_alerts,
        "verify-journal": _cmd_verify,
        "doctor": _cmd_doctor,
    }
    try:
        return handlers[args.command](args)
    except TigerQuantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
