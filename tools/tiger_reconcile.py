"""Read the verified Tiger paper account and reconcile it against our ledger.

    python tools/tiger_reconcile.py                 # read, reconcile, report
    python tools/tiger_reconcile.py --json out.json  # also write a machine-readable record

Read-only. This tool imports no write path and calls no write API; the credential it uses
holds read-only scopes, so it could not place an order even if it tried.

Opening balance: at the start of a paper session the ledger adopts the broker's reported
cash, and the tool says so explicitly rather than pretending to have known it in advance.
Pass ``--opening-cash`` to compare against a specific figure instead — which is what a
restart mid-session should do, using the cash recorded in the journal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tigerquant.adapters.tiger.guard import TigerAccountGuard  # noqa: E402
from tigerquant.adapters.tiger.oauth import (  # noqa: E402
    TigerEnvironment,
    TigerQuantOAuthSession,
    build_oauth2_config,
    default_token_home,
)
from tigerquant.adapters.tiger.read import TigerReadOnlyClient, reconcile  # noqa: E402
from tigerquant.core.errors import TigerQuantError  # noqa: E402
from tigerquant.portfolio.ledger import PortfolioLedger  # noqa: E402


def _emit(key: str, value: str) -> None:
    print(f"{key}{value}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--opening-cash", default=None, help="ledger cash to compare against")
    parser.add_argument("--json", type=Path, default=None, help="write the report here")
    args = parser.parse_args(argv)

    account_file = default_token_home() / "account"
    if not account_file.is_file():
        _emit("ERROR=", "no verified paper account stored; run tools/tiger_login.py --select-paper")
        return 2
    account = account_file.read_text(encoding="utf-8").strip()

    try:
        session = TigerQuantOAuthSession()
        if not session.is_authorized():
            _emit("ERROR=", "not authorized; run tools/tiger_login.py first")
            return 2
        from tigeropen.trade.trade_client import TradeClient

        config = build_oauth2_config(session, environment=TigerEnvironment.GATEWAY, account=account)
        sdk_client = TradeClient(config)
        guard = TigerAccountGuard(requested_account=account)
        client = TigerReadOnlyClient(sdk_client, guard, account=account)

        snapshot = client.snapshot(as_of=dt.datetime.now(tz=dt.UTC))
    except TigerQuantError as exc:
        _emit("ERROR=", str(exc))
        return 2
    except Exception as exc:
        _emit("ERROR=", f"{type(exc).__name__}: {exc}")
        return 2

    _emit("ACCOUNT=", client.masked_account)
    _emit("CURRENCY=", snapshot.currency)
    _emit("CASH=", str(snapshot.cash))
    _emit("NET_LIQUIDATION=", str(snapshot.net_liquidation))
    _emit("BUYING_POWER=", str(snapshot.buying_power))
    _emit("GROSS_POSITION_VALUE=", str(snapshot.gross_position_value))
    _emit("POSITIONS=", str(len(snapshot.positions)))
    for position in snapshot.positions:
        _emit("POSITION=", json.dumps(position.as_record(), sort_keys=True))
    _emit("OPEN_ORDERS=", str(len(snapshot.open_orders)))
    for order in snapshot.open_orders:
        _emit("ORDER=", json.dumps(order.as_record(), sort_keys=True))
    if snapshot.missing_fields:
        _emit("MISSING_FIELDS=", ",".join(snapshot.missing_fields))

    if args.opening_cash is not None:
        opening = Decimal(args.opening_cash)
        _emit("OPENING_CASH_SOURCE=", "explicit (--opening-cash)")
    else:
        opening = snapshot.cash
        _emit(
            "OPENING_CASH_SOURCE=",
            "adopted from the broker, because no journal cash was supplied. A mid-session "
            "restart must pass --opening-cash from the journal instead.",
        )
    _emit("OPENING_CASH=", str(opening))

    ledger = PortfolioLedger(initial_cash=opening)
    # Broker positions become the ledger's opening positions: at session start the broker is
    # the record of what is held, and the ledger is empty until fills arrive.
    report = reconcile(snapshot, ledger)
    _emit("RECONCILED=", "true" if report.is_reconciled else "false")
    _emit("RECONCILIATION=", report.summary())

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {"snapshot": snapshot.as_record(), "reconciliation": report.as_record()},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        _emit("REPORT_WRITTEN=", str(args.json))

    return 0 if report.is_reconciled else 1


if __name__ == "__main__":
    raise SystemExit(main())
