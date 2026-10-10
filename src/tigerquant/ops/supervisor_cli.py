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
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.enums import Mode
from tigerquant.ops.alerts import AlertRouter
from tigerquant.ops.supervisor import Supervisor, SupervisorConfig, SupervisorReport

if TYPE_CHECKING:
    import argparse

__all__ = ["DEFAULT_HEARTBEAT", "build_cycle", "cmd_supervise"]

#: Where the liveness record goes by default. A path rather than a flag-with-no-default, because
#: a supervisor nobody can tell is stuck is the problem this command exists to avoid.
DEFAULT_HEARTBEAT = Path("out/supervisor.json")


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

    router: AlertRouter | None = None
    if not args.no_alerts:
        router = build_router(
            log_path=args.alert_log,
            webhook=args.webhook,
            floor=_severity(args.floor),
        )

    cycle, monitor = build_cycle(args.journal, mode=Mode(args.mode))

    config = SupervisorConfig(
        interval_seconds=args.interval,
        max_consecutive_failures=args.max_failures,
        heartbeat_path=args.heartbeat,
        max_cycles=args.iterations,
    )
    supervisor = Supervisor(cycle, config=config, router=router)

    print(f"supervising {args.journal}")
    print(f"  interval          {args.interval}s")
    print(f"  max failures      {args.max_failures} consecutive, then halt")
    print(f"  heartbeat         {args.heartbeat}")
    print(f"  cycles            {'until stopped' if args.iterations is None else args.iterations}")
    print(f"  database          {_describe_database()}")
    print()
    if not args.quiet:
        print(
            "  NOTE: this reconciles against an IN-MEMORY database, so it is a wiring check\n"
            "        rather than a live reconciliation. Pointing it at PostgreSQL is not\n"
            "        implemented. Stop with Ctrl-C; the last heartbeat stays on disk.\n"
        )

    try:
        report = supervisor.run()
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

    # Exit 2 on a halt, so a supervisor can be supervised by whatever started it. Exiting 0 would
    # make a self-halted loop indistinguishable from a clean shutdown.
    return 2 if report.halted else 0


def _describe_database() -> str:
    return "in-memory (wiring check only; PostgreSQL is not wired to this command)"


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
    supervise.add_argument("--no-alerts", action="store_true", help="run without routing alerts")
    supervise.add_argument("--alert-log", type=Path, default=None, help="file sink for alerts")
    supervise.add_argument("--webhook", default=None, help="http(s) endpoint for alert delivery")
    supervise.add_argument("--floor", default="WARNING", help="minimum severity to deliver")
    supervise.add_argument("--quiet", action="store_true", help="suppress the banner")
