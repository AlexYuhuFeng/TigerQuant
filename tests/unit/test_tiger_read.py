"""Read-only Tiger client and broker/ledger reconciliation.

The fixture shapes are the ones the live paper account actually returned on 2026-10-10,
including the detail that cost the most to find: **Tiger's paper environment returns ``inf``
for figures it does not compute.** ``get_assets()`` reported ``cash = 1000000.0`` alongside
``gross_position_value = inf`` and ``available_funds = inf``, and ``get_assets()`` returned a
plain list rather than the ``response.assets`` envelope the SDK docstring describes.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.adapters.tiger.guard import TigerAccountGuard
from tigerquant.adapters.tiger.read import (
    WRITE_API_NAMES,
    BrokerBalances,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    TigerReadOnlyClient,
    broker_decimal,
    reconcile,
)
from tigerquant.core.enums import OrderSide, OrderStatus, ReasonCode
from tigerquant.core.errors import BrokerError
from tigerquant.portfolio.ledger import PortfolioLedger

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
PAPER_ACCOUNT = "20240101120000042"


# ---------------------------------------------------------------------------
# Fakes that mirror the live response shapes.
# ---------------------------------------------------------------------------
class _Summary:
    def __init__(self, **fields: object) -> None:
        # Observed live: uncomputed fields arrive as float('inf'), not as None.
        defaults: dict[str, object] = {
            "cash": 1_000_000.0,
            "net_liquidation": 1_000_000.0,
            "buying_power": 4_000_000.0,
            "gross_position_value": float("inf"),
            "available_funds": float("inf"),
            "currency": "USD",
        }
        defaults.update(fields)
        for key, value in defaults.items():
            setattr(self, key, value)


class _PortfolioAccount:
    def __init__(self, summary: _Summary) -> None:
        self.account = PAPER_ACCOUNT
        self.summary = summary


class _Contract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class _Position:
    def __init__(self, symbol: str, quantity: int, **fields: object) -> None:
        self.contract = _Contract(symbol)
        self.quantity = quantity
        self.average_cost = fields.get("average_cost", 600.0)
        self.market_price = fields.get("market_price", 601.0)
        self.market_value = fields.get("market_value", 4808.0)
        self.unrealized_pnl = fields.get("unrealized_pnl", 8.0)
        self.realized_pnl = fields.get("realized_pnl", 0.0)


class _Order:
    def __init__(self, **fields: object) -> None:
        self.order_id = fields.get("order_id", "SIM-1")
        self.contract = _Contract(str(fields.get("symbol", "SPY")))
        self.action = fields.get("action", "BUY")
        self.quantity = fields.get("quantity", 8)
        self.filled = fields.get("filled", 0)
        self.remaining = fields.get("remaining", 8)
        self.avg_fill_price = fields.get("avg_fill_price", 0.0)
        self.limit_price = fields.get("limit_price")
        self.order_type = fields.get("order_type", "MKT")
        self.time_in_force = fields.get("time_in_force", "DAY")
        self.status = fields.get("status", "Submitted")


class _Profile:
    def __init__(self) -> None:
        self.account = PAPER_ACCOUNT
        self.capability = "RegTMargin"
        self.status = "Funded"
        self.account_type = "PAPER"


class _Response:
    def __init__(self, profiles: list[object]) -> None:
        self.profiles = profiles


class _FakeTradeClient:
    """Reads only. Deliberately has no write method, mirroring the client wrapper's contract."""

    def __init__(
        self,
        *,
        assets: list[object] | None = None,
        positions: list[object] | None = None,
        orders: list[object] | None = None,
    ) -> None:
        self._assets = assets if assets is not None else [_PortfolioAccount(_Summary())]
        self._positions = positions or []
        self._orders = orders or []

    def get_managed_accounts(self) -> _Response:
        return _Response([_Profile()])

    def get_assets(self, account: str | None = None) -> list[object]:
        return self._assets

    def get_positions(self, *args: object, **kwargs: object) -> list[object]:
        return self._positions

    def get_open_orders(self, *args: object, **kwargs: object) -> list[object]:
        return self._orders


def make_client(client: _FakeTradeClient | None = None) -> TigerReadOnlyClient:
    sdk = client or _FakeTradeClient()
    guard = TigerAccountGuard(requested_account=PAPER_ACCOUNT)
    return TigerReadOnlyClient(sdk, guard, account=PAPER_ACCOUNT)


class TestBrokerDecimal:
    def test_exact_conversion_of_a_float(self) -> None:
        assert broker_decimal(1_000_000.0) == Decimal("1000000.0")
        assert broker_decimal(0.1) == Decimal("0.1")

    def test_infinities_are_absence_not_extreme_values(self) -> None:
        """The finding that mattered: inf means "not computed", never "very large"."""
        assert broker_decimal(float("inf")) is None
        assert broker_decimal(float("-inf")) is None

    def test_nan_is_absence(self) -> None:
        assert broker_decimal(float("nan")) is None

    def test_absent_and_empty_are_absence(self) -> None:
        assert broker_decimal(None) is None
        assert broker_decimal("") is None

    def test_string_decimals_are_exact(self) -> None:
        assert broker_decimal("600.125") == Decimal("600.125")

    def test_non_finite_decimal_is_absence(self) -> None:
        assert broker_decimal(Decimal("Infinity")) is None

    def test_bool_is_rejected_rather_than_read_as_one(self) -> None:
        with pytest.raises(BrokerError, match="bool is not a broker number"):
            broker_decimal(True)

    def test_unparseable_value_raises(self) -> None:
        with pytest.raises(BrokerError, match="cannot interpret"):
            broker_decimal("not-a-number")


class TestStructuralReadOnly:
    def test_no_public_member_is_named_after_a_write_api(self) -> None:
        members = {name for name in dir(TigerReadOnlyClient) if not name.startswith("_")}
        assert not (members & WRITE_API_NAMES), (
            f"the read-only client exposes {sorted(members & WRITE_API_NAMES)}"
        )

    def test_the_guard_refuses_before_any_read(self) -> None:
        sdk = _FakeTradeClient()
        sdk.get_managed_accounts = lambda: _Response(  # type: ignore[method-assign]
            [
                type(
                    "P",
                    (),
                    {
                        "account": "7654321",
                        "capability": "X",
                        "status": "Funded",
                        "account_type": "STANDARD",
                    },
                )()
            ]
        )
        client = TigerReadOnlyClient(
            sdk, TigerAccountGuard(requested_account="7654321"), account="7654321"
        )
        with pytest.raises(BrokerError, match="verification refused"):
            client.snapshot(as_of=AT)


class TestAssets:
    def test_reads_the_plain_list_shape(self) -> None:
        balances = make_client().assets()
        assert balances.cash == Decimal("1000000.0")
        assert balances.net_liquidation == Decimal("1000000.0")
        assert balances.currency == "USD"

    def test_infinite_fields_are_recorded_as_missing_not_zeroed_silently(self) -> None:
        balances = make_client().assets()
        assert balances.gross_position_value == Decimal(0)
        assert balances.available_funds == Decimal(0)
        # The point: the caller can tell "not reported" from "actually zero".
        assert set(balances.missing_fields) == {"gross_position_value", "available_funds"}

    def test_no_assets_is_a_broker_error(self) -> None:
        with pytest.raises(BrokerError, match="no asset record"):
            make_client(_FakeTradeClient(assets=[])).assets()

    def test_balances_record_is_audit_safe(self) -> None:
        record = BrokerBalances(
            currency="USD",
            cash=Decimal(1),
            net_liquidation=Decimal(1),
            buying_power=Decimal(1),
            gross_position_value=Decimal(0),
            available_funds=Decimal(0),
        ).as_record()
        assert record["cash"] == "1"
        assert record["missing_fields"] == []


class TestPositions:
    def test_reads_positions_sorted_by_instrument(self) -> None:
        sdk = _FakeTradeClient(
            positions=[_Position("SPY", 8), _Position("AAPL", 3)],
        )
        positions = make_client(sdk).positions()
        assert [p.instrument for p in positions] == ["AAPL", "SPY"]
        assert positions[1].quantity == 8

    def test_zero_quantity_rows_are_dropped(self) -> None:
        sdk = _FakeTradeClient(positions=[_Position("SPY", 0), _Position("QQQ", 2)])
        assert [p.instrument for p in make_client(sdk).positions()] == ["QQQ"]

    def test_rows_without_a_symbol_are_skipped(self) -> None:
        orphan = type("R", (), {"contract": None, "quantity": 5})()
        sdk = _FakeTradeClient(positions=[orphan, _Position("SPY", 1)])
        assert [p.instrument for p in make_client(sdk).positions()] == ["SPY"]


class TestOrders:
    def test_reads_the_plain_list_shape_and_maps_status(self) -> None:
        sdk = _FakeTradeClient(orders=[_Order(status="PartiallyFilled", filled=4, remaining=4)])
        orders = make_client(sdk).open_orders()
        assert len(orders) == 1
        assert orders[0].status is OrderStatus.PARTIALLY_FILLED
        assert orders[0].raw_status == "PartiallyFilled"
        assert orders[0].remaining_quantity == 4

    def test_an_unmapped_status_is_preserved_verbatim(self) -> None:
        sdk = _FakeTradeClient(orders=[_Order(status="SomethingNew")])
        order = make_client(sdk).open_orders()[0]
        assert order.status is None
        assert order.raw_status == "SomethingNew"
        assert order.as_record()["mapped_status"] == "(unmapped)"

    def test_limit_price_absent_stays_none(self) -> None:
        sdk = _FakeTradeClient(orders=[_Order(limit_price=None)])
        assert make_client(sdk).open_orders()[0].limit_price is None

    def test_terminal_orders_are_not_open(self) -> None:
        filled = BrokerOrder(
            broker_order_id="x",
            instrument="SPY",
            side=OrderSide.BUY,
            quantity=8,
            filled_quantity=8,
            remaining_quantity=0,
            average_fill_price=Decimal(600),
            limit_price=None,
            order_type="MKT",
            time_in_force="DAY",
            raw_status="Filled",
            status=OrderStatus.FILLED,
        )
        assert not filled.is_open


class TestSnapshot:
    def test_snapshot_reads_everything_and_masks_the_account(self) -> None:
        sdk = _FakeTradeClient(
            positions=[_Position("SPY", 8)],
            orders=[_Order()],
        )
        snapshot = make_client(sdk).snapshot(as_of=AT)
        assert snapshot.account == PAPER_ACCOUNT
        assert snapshot.cash == Decimal("1000000.0")
        assert len(snapshot.positions) == 1
        assert len(snapshot.open_orders) == 1
        record = snapshot.as_record()
        assert PAPER_ACCOUNT not in str(record)
        assert record["account"].endswith("0042")

    def test_position_lookup(self) -> None:
        sdk = _FakeTradeClient(positions=[_Position("SPY", 8)])
        snapshot = make_client(sdk).snapshot(as_of=AT)
        assert snapshot.position("SPY") is not None
        assert snapshot.position("QQQ") is None


class TestReconciliation:
    def _snapshot(self, *, cash: str, positions: list[BrokerPosition]) -> BrokerSnapshot:
        return BrokerSnapshot(
            as_of=AT,
            account=PAPER_ACCOUNT,
            cash=Decimal(cash),
            net_liquidation=Decimal(cash),
            buying_power=Decimal(cash),
            gross_position_value=Decimal(0),
            currency="USD",
            positions=tuple(positions),
            open_orders=(),
        )

    def _broker_position(self, instrument: str, quantity: int) -> BrokerPosition:
        return BrokerPosition(
            instrument=instrument,
            quantity=quantity,
            average_cost=Decimal(600),
            market_price=Decimal(601),
            market_value=Decimal(4808),
            unrealized_pnl=Decimal(8),
            realized_pnl=Decimal(0),
        )

    def test_flat_and_matching_cash_reconciles(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        report = reconcile(self._snapshot(cash="1000000.00", positions=[]), ledger)
        assert report.is_reconciled
        assert report.as_reason() is ReasonCode.SYMBOL_NOT_ALLOWED  # unused on success
        assert "reconciled with" in report.summary()

    def test_matching_position_reconciles(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        ledger.apply_fill(
            exec_id="e1",
            instrument="SPY",
            side=OrderSide.BUY,
            quantity=8,
            price=Decimal(600),
            occurred_at=AT,
        )
        report = reconcile(
            self._snapshot(cash=str(ledger.cash), positions=[self._broker_position("SPY", 8)]),
            ledger,
        )
        assert report.is_reconciled
        assert report.matched == ("SPY",)

    def test_a_quantity_mismatch_is_reported_and_not_corrected(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        ledger.apply_fill(
            exec_id="e1",
            instrument="SPY",
            side=OrderSide.BUY,
            quantity=8,
            price=Decimal(600),
            occurred_at=AT,
        )
        before = ledger.digest()
        report = reconcile(
            self._snapshot(cash=str(ledger.cash), positions=[self._broker_position("SPY", 5)]),
            ledger,
        )
        assert not report.is_reconciled
        assert report.as_reason() is ReasonCode.UNRECONCILED
        assert report.differences[0].delta == -3
        # The ledger is untouched: a mismatch is evidence, not something to paper over.
        assert ledger.digest() == before
        assert "NOT RECONCILED" in report.summary()

    def test_a_position_held_only_at_the_broker_is_reported(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        report = reconcile(
            self._snapshot(cash=str(ledger.cash), positions=[self._broker_position("SPY", 8)]),
            ledger,
        )
        assert report.broker_only == ("SPY",)
        assert not report.is_reconciled

    def test_a_position_only_in_the_ledger_is_reported(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        ledger.apply_fill(
            exec_id="e1",
            instrument="SPY",
            side=OrderSide.BUY,
            quantity=8,
            price=Decimal(600),
            occurred_at=AT,
        )
        report = reconcile(self._snapshot(cash=str(ledger.cash), positions=[]), ledger)
        assert report.ledger_only == ("SPY",)
        assert not report.is_reconciled

    def test_a_cash_difference_alone_breaks_reconciliation(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        report = reconcile(
            self._snapshot(cash="999999.00", positions=[]),
            ledger,
        )
        assert not report.is_reconciled
        assert report.cash_difference == Decimal("-1.00")
        assert "cash differs" in report.summary()

    def test_record_is_audit_safe(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        ledger.apply_fill(
            exec_id="e1",
            instrument="SPY",
            side=OrderSide.BUY,
            quantity=8,
            price=Decimal(600),
            occurred_at=AT,
        )
        # A difference needs both sides to hold the instrument; a broker-only holding is
        # reported separately as broker_only.
        report = reconcile(
            self._snapshot(cash=str(ledger.cash), positions=[self._broker_position("SPY", 5)]),
            ledger,
        )
        record = report.as_record()
        assert PAPER_ACCOUNT not in str(record)
        assert record["reconciled"] is False
        assert record["differences"][0]["delta"] == -3

    def test_broker_only_record_is_audit_safe(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        report = reconcile(
            self._snapshot(cash=str(ledger.cash), positions=[self._broker_position("SPY", 5)]),
            ledger,
        )
        record = report.as_record()
        assert record["broker_only"] == ["SPY"]
        assert record["differences"] == []
        assert PAPER_ACCOUNT not in str(record)

    def test_universe_filter_excludes_out_of_universe_instruments(self) -> None:
        ledger = PortfolioLedger(initial_cash=Decimal("1000000.00"))
        report = reconcile(
            self._snapshot(
                cash=str(ledger.cash),
                positions=[self._broker_position("TSLA", 3), self._broker_position("SPY", 0)],
            ),
            ledger,
            instrument_filter=["SPY"],
        )
        assert report.is_reconciled
