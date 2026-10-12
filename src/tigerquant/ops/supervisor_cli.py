"""The `tigerquant supervise` command: run the observation loop as a long-lived process.

`ops/supervisor.py` had no way to be started. This is the entry point, and it exists so the loop
can be run, watched and stopped from a shell rather than only from Python.

Two things about it are deliberate and worth stating up front.

**It cannot trade, and the command has no flag that would let it.** There is no broker option, no
account option and no approval registry anywhere in this module. The cycle it builds only ever
READS a journal and compares it with a database. That is enforced by what is absent, which is a
stronger guarantee than a check that could be removed.

**Against an in-memory repository it is a WIRING DEMONSTRATION, not a live reconciliation.** The
command says so in its own output rather than leaving the operator to infer it from the flags.
Pointing this at PostgreSQL is what would make it real, and that is not implemented -- the same
limitation `tigerquant alerts drift` carries, for the same reason.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.enums import Mode
from tigerquant.ops.alerts import AlertRouter
from tigerquant.ops.supervisor import (
    BackoffPolicy,
    Supervisor,
    SupervisorConfig,
    SupervisorReport,
)

if TYPE_CHECKING:
    import argparse

__all__ = ["DEFAULT_HEARTBEAT", "DSN_ENV", "build_cycle", "cmd_supervise"]

#: Where the liveness record goes by default. A path rather than a flag-with-no-default, because
#: a supervisor nobody can tell is stuck is the problem this command exists to avoid.
DEFAULT_HEARTBEAT = Path("out/supervisor.json")

#: Set this to reconcile against PostgreSQL without passing --dsn. Unset means in-memory,
#: which the command reports as a wiring check rather than quietly treating as real.
DSN_ENV = "TIGERQUANT_DB_DSN"


def build_cycle(
    journal_path: Path,
    *,
    mode: Mode = Mode.BACKTEST,
    repository: object | None = None,
) -> tuple[Callable[[], bool], Any]:
    """A callable that reconciles the journal against the repository, and the monitor behind it.

    Returns both so the caller can inspect the monitor's history afterwards. The callable returns
    True when it found a divergence, which the supervisor records as a SUCCESS that reports bad
    news -- see `SuervisorReport`; a run that found drift every cycle is a healthy run.
    """
    from tigerquant.db.repository import InMemoryRepository
    from tigerquant.events.journal import AppendOnlyJournal
    from tigerquant.ops.drift_monitor import DriftMonitor

    repo = repository if repository is not None else InMemoryRepository()
    monitor = DriftMonitor(repo)  # type: ignore[arg-type]

    def cycle() -> bool:
        # Re-read the journal every cycle: a supervisor that cached it would be reconciling a
        # snapshot of the past while reporting on the present, which is worse than not running.
        with AppendOnlyJournal(journal_path, mode, fsync=False) as journal:
            events = [record.event for record in journal.iter_records()]
        result = monitor.check(events, at=dt.datetime.now(tz=dt.UTC))
        return result.consistent is False

    return cycle, monitor


def cmd_supervise(args: argparse.Namespace) -> int:
    """Build the loop, attach alerts, run it, and report what happened."""
    from tigerquant.ops.alert_cli import _severity, build_router

    if not args.journal.is_file():
        print(f"error: journal not found: {args.journal}", file=sys.stderr)
        return 2

    connection, repository, failure = _select_repository(args)
    if failure:
        return failure

    router: AlertRouter | None = None
    if not args.no_alerts:
        router = build_router(
            log_path=args.alert_log,
            webhook=args.webhook,
            floor=_severity(args.floor),
        )

    cycle, monitor = build_cycle(args.journal, mode=Mode(args.mode), repository=repository)

    config = SupervisorConfig(
        interval_seconds=args.interval,
        max_consecutive_failures=args.max_failures,
        heartbeat_path=args.heartbeat,
        max_cycles=args.iterations,
        backoff=BackoffPolicy(
            initial_seconds=min(1.0, args.interval),
            max_seconds=args.retry_max_seconds,
            max_attempts=args.retry_attempts,
        ),
    )
    supervisor = Supervisor(cycle, config=config, router=router, is_transient=is_transient_failure)

    print(f"supervising {args.journal}")
    print(f"  interval          {args.interval}s")
    print(f"  max failures      {args.max_failures} consecutive, then halt")
    print(f"  heartbeat         {args.heartbeat}")
    print(f"  cycles            {'until stopped' if args.iterations is None else args.iterations}")
    print(
        f"  transient retry   {args.retry_attempts} attempt(s), backoff ceiling "
        f"{args.retry_max_seconds}s. Only database-shaped failures are retried; anything "
        "else halts at once."
    )
    print(f"  database          {_describe_database(repository)}")
    print()
    if not args.quiet:
        if repository is None:
            print(
                "  NOTE: no database given, so this reconciles against an IN-MEMORY repository.\n"
                f"        That is a WIRING CHECK, not a live reconciliation. Pass --dsn or set\n"
                f"        {DSN_ENV} to reconcile against PostgreSQL.\n"
                "        Stop with Ctrl-C; the last heartbeat stays on disk.\n"
            )
        else:
            print(
                "  Reconciling against PostgreSQL. Nothing here repairs a divergence: we do not\n"
                "  know which record is wrong, so overwriting either would destroy the evidence\n"
                "  needed to find out. Stop with Ctrl-C; the last heartbeat stays on disk.\n"
            )

    try:
        report = _run_supervisor(supervisor)
    except KeyboardInterrupt:
        # Ctrl-C is the normal way to stop a long-running process, so it is not an error. The
        # report is built from whatever cycles completed.
        print("\nstopped by request")
        outcomes = supervisor.outcomes
        report = SupervisorReport(
            cycles=len(outcomes),
            ok_cycles=sum(1 for outcome in outcomes if outcome.ok),
            failed_cycles=sum(1 for outcome in outcomes if not outcome.ok),
            outcomes=outcomes,
        )

    print()
    print(report.summary())
    print(f"  digest {report.digest()}")

    drift_cycles = sum(1 for outcome in report.outcomes if outcome.drift_detected)
    if drift_cycles:
        print(
            f"  {drift_cycles} cycle(s) reported a divergence. That is a SUCCESSFUL check "
            "reporting bad news, not a failing supervisor."
        )
    print(f"  monitor history: {len(monitor.history)} check(s)")

    if connection is not None:
        # The driver's connection type is not imported here on purpose -- the db extra is
        # optional -- so close() is called through a narrow protocol rather than an isinstance.
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    # Exit 2 on a halt, so a supervisor can be supervised by whatever started it. Exiting 0 would
    # make a self-halted loop indistinguishable from a clean shutdown.
    return 2 if report.halted else 0


#: Exception types that mean "the database is not reachable right now" rather than "the code is
#: wrong". Deliberately a small, explicit set: anything not listed here is PERMANENT and halts.
_TRANSIENT_NAMES = frozenset(
    {
        "OperationalError",
        "ConnectionTimeout",
        "ConnectionRefused",
        "InterfaceError",
        "AdminShutdown",
        "CannotConnectNow",
        "TooManyConnections",
    }
)


def is_transient_failure(exc: BaseException) -> bool:
    """Whether a failed cycle is worth RETRYING.

    Matched by exception NAME rather than by importing the driver's hierarchy, because the `db`
    extra is optional and this module must import without it. Name matching is weaker than an
    isinstance check and that is stated rather than hidden -- the mitigation is that the set is
    small and explicit, and anything unlisted is treated as permanent.
    """
    for klass in type(exc).__mro__:
        if klass.__name__ in _TRANSIENT_NAMES:
            return True
    # psycopg wraps the real cause; a connection error inside an otherwise generic exception is
    # still a connection error.
    cause = exc.__cause__
    return cause is not None and cause is not exc and is_transient_failure(cause)


def _select_repository(
    args: argparse.Namespace,
) -> tuple[object | None, object | None, int | None]:
    """(connection, repository, failure_code).

    Extracted so `cmd_supervise` reads as a sequence of decisions rather than as a sequence of
    except clauses, and so the refusal logic is testable on its own.

    A connection failure is a REFUSAL, never a fallback to in-memory: a supervisor that appears to
    reconcile while actually comparing against an empty dict is worse than one that will not start,
    because it reports health it has not checked.
    """
    dsn = getattr(args, "dsn", None) or os.environ.get(DSN_ENV)
    if not dsn:
        return None, None, None

    from tigerquant.core.errors import IntegrationUnavailable, TigerQuantError
    from tigerquant.db.postgres import PostgresRepository, connect

    try:
        connection = connect(dsn)
    except (IntegrationUnavailable, TigerQuantError) as exc:
        print(f"cannot reach the database: {exc}", file=sys.stderr)
        return None, None, 2
    except Exception as exc:  # noqa: BLE001 - the driver's exception types are many
        # The driver raises its OWN hierarchy, which the clause above does not catch. Without
        # this, an unreachable database produced a TRACEBACK and exit 1 -- and exit 1 from a
        # traceback is indistinguishable from a crash for any other reason, which defeats the
        # point of a distinct code for "the database is down". Caught broadly ON PURPOSE,
        # reported narrowly.
        print(
            f"cannot reach the database: {type(exc).__name__}: {str(exc)[:200]}",
            file=sys.stderr,
        )
        return None, None, 2
    return connection, PostgresRepository(connection, mode=Mode(args.mode)), None


def _run_supervisor(supervisor: Supervisor) -> SupervisorReport:
    """Run it. Extracted so the Ctrl-C handler reads as a handler rather than a nested block."""
    return supervisor.run()


def _describe_database(repository: object | None) -> str:
    """Name the repository, because "which database" is the first thing an operator asks."""
    if repository is None:
        return "in-memory (WIRING CHECK ONLY -- pass --dsn for a real reconciliation)"
    return f"PostgreSQL via {type(repository).__name__}"


def supervise_parser(sub: object) -> None:
    """Register the `supervise` subcommand.

    Kept beside the handler so the flags and the code that reads them cannot drift apart.
    """
    supervise = sub.add_parser(  # type: ignore[attr-defined]
        "supervise",
        help=(
            "run the observation loop: reconcile a journal against a database on a cadence, "
            "with a heartbeat and a self-halt after repeated failures. CANNOT SUBMIT ORDERS."
        ),
    )
    supervise.add_argument("journal", type=Path, help="audit journal JSONL to reconcile")
    supervise.add_argument(
        "--mode",
        default=Mode.BACKTEST.value,
        choices=[mode.value for mode in Mode],
        help="the mode this journal belongs to",
    )
    supervise.add_argument("--interval", type=float, default=30.0, help="seconds between cycles")
    supervise.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="stop after this many cycles; omit to run until stopped",
    )
    supervise.add_argument(
        "--max-failures",
        type=int,
        default=3,
        help="consecutive failing cycles before halting. A loop that keeps running against a "
        "broken system is the state most easily mistaken for health.",
    )
    supervise.add_argument(
        "--heartbeat",
        type=Path,
        default=DEFAULT_HEARTBEAT,
        help="liveness file, rewritten twice per cycle so a watchdog can see the cycle index",
    )
    supervise.add_argument(
        "--dsn",
        default=None,
        help=(
            "PostgreSQL DSN. Omit to reconcile against an in-memory repository, which is a "
            f"WIRING CHECK and not a live reconciliation. {DSN_ENV} is the environment equivalent."
        ),
    )
    supervise.add_argument(
        "--retry-attempts",
        type=int,
        default=8,
        help=(
            "consecutive TRANSIENT failures to retry with exponential backoff before halting. "
            "A database that blinks is retried; anything else halts immediately, because retrying "
            "a defect for ever looks like health."
        ),
    )
    supervise.add_argument(
        "--retry-max-seconds",
        type=float,
        default=60.0,
        help="ceiling on the backoff delay, so a long outage does not produce retries hours apart",
    )
    supervise.add_argument("--no-alerts", action="store_true", help="run without routing alerts")
    supervise.add_argument("--alert-log", type=Path, default=None, help="file sink for alerts")
    supervise.add_argument("--webhook", default=None, help="http(s) endpoint for alert delivery")
    supervise.add_argument("--floor", default="WARNING", help="minimum severity to deliver")
    supervise.add_argument("--quiet", action="store_true", help="suppress the banner")
