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
from pathlib import Path
from typing import TYPE_CHECKING

from tigerquant import IMPLEMENTATION_STATUS, __version__
from tigerquant.core.enums import Mode, capabilities_for
from tigerquant.core.errors import IntegrationUnavailable, TigerQuantError
from tigerquant.db.postgres import PostgresRepository, connect
from tigerquant.db.projector import check_drift, project
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.events.registry import event_type_names
from tigerquant.market.fixtures import load_scenario
from tigerquant.pipeline.replay import run_replay

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_parser", "main"]


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
    offenders: list[str] = []
    for path in source_files:
        if path.name in _CLOCK_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in banned_calls:
                offenders.append(f"{path.name}:{func.id}()")
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if (func.value.id, func.attr) in banned_dotted:
                    offenders.append(f"{path.name}:{func.value.id}.{func.attr}()")
    return offenders


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
