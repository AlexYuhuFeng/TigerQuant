"""Apply TigerQuant migrations to a database, or report their status.

Usage::

    python tools/db_migrate.py --dsn postgresql://user:pw@127.0.0.1:5432/tq_backtest
    python tools/db_migrate.py --mode backtest --status
    python tools/db_migrate.py --mode backtest --dry-run

The DSN may also come from ``TIGERQUANT_DB_DSN``. It is never logged: the tool prints the
database name and host only, because a DSN contains a password.

One database per mode (ADR-0013 §2), so ``--mode`` builds the loopback development DSN for
that mode's own database and role. Every mode is migrated by the same code path.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tigerquant.core.errors import TigerQuantError  # noqa: E402
from tigerquant.db.migrations import MigrationRunner, content_digest, discover_migrations  # noqa: E402

#: Development-only credentials, matching deploy/compose.yaml and the init script. They open
#: a loopback-only database and are never used outside local development.
DEV_PASSWORD = "tq_local_development_only"
DEV_HOST = "127.0.0.1"
DEV_PORT = "5432"
MODES = ("backtest", "shadow", "paper", "live")


def dev_dsn(mode: str) -> str:
    if mode not in MODES:
        msg = f"unknown mode {mode!r}; expected one of {', '.join(MODES)}"
        raise SystemExit(msg)
    role = f"tq_{mode}"
    return f"postgresql://{role}:{DEV_PASSWORD}@{DEV_HOST}:{DEV_PORT}/{role}"


def describe_dsn(dsn: str) -> str:
    """Render a DSN without its password."""
    head, _, tail = dsn.rpartition("@")
    if not tail:
        return "(unparseable DSN)"
    scheme, _, credentials = head.partition("://")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=None, help="full DSN; overrides --mode")
    parser.add_argument("--mode", choices=MODES, default="backtest")
    parser.add_argument("--status", action="store_true", help="report state, apply nothing")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, apply nothing")
    args = parser.parse_args(argv)

    dsn = args.dsn or os.environ.get("TIGERQUANT_DB_DSN") or dev_dsn(args.mode)
    print(f"target: {describe_dsn(dsn)}", flush=True)

    migrations = discover_migrations()
    print(f"migrations: {len(migrations)} ({content_digest(migrations)[:26]}…)", flush=True)

    try:
        from tigerquant.db.postgres import connect
    except TigerQuantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        with connect(dsn) as connection:
            runner = MigrationRunner(migrations)
            if args.status:
                print(runner.status(connection))
                return 0
            if args.dry_run:
                planned = runner.apply(connection, dry_run=True)
                print(f"would apply {len(planned)}: {[m.label for m in planned]}")
                return 0
            applied = runner.apply(connection)
            if applied:
                print(f"applied {len(applied)}: {[m.label for m in applied]}")
            else:
                print("already up to date")
            print(runner.status(connection))
    except TigerQuantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
