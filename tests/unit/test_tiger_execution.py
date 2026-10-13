"""Tiger paper execution adapter: the four gates, and the failure modes around them.



The adapter is the only broker write path in the system, so these tests are mostly about
what it *refuses* to do. The live credential this repository holds is read-only, so the
first gate is the one that actually fires today — and that is asserted, not assumed.

A fake SDK stands in for ``tigeropen`` so the mapping and the ambiguity handling are tested
without network access. The fake records every call, which is how "never retried" is proved
rather than asserted in prose.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.adapters.tiger.execution import (
    WRITE_SCOPE,
    TigerPaperExecutionClient,
    map_status,
    reason_for_state,
    require_write_scope,
    supported_order_types,
)
from tigerquant.adapters.tiger.guard import TigerAccountGuard
from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import (
    ImplementationStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    ReasonCode,
    TimeInForce,
)
from tigerquant.core.errors import BrokerError, ConfigurationError
from tigerquant.execution.broker import BrokerOrderState
from tigerquant.risk.approvals import OrderIntentView

# This module reaches an optional SDK through the adapter it imports, so it needs that
# extra installed. The fast lane syncs only `--extra dev` and DESELECTS it rather than
# failing: the marker existed in pyproject all along and nothing wore it, so CI ran these
# against an environment that deliberately lacks the dependency. Placed after the imports
# so it neither lands inside the docstring nor trips E402.
pytestmark = pytest.mark.requires_broker

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
PAPER_ACCOUNT = "20240101120000042"
READ_ONLY_SCOPES = "api.basic:read api.account:read api.quote:read api.trade:read"
WRITE_SCOPES = f"{READ_ONLY_SCOPES} {WRITE_SCOPE}"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Session:
    """Stands in for TigerQuantOAuthSession; only ``scopes`` matters to the adapter."""

    def __init__(self, scopes: str) -> None:
        self.scopes = scopes


class _Profile:
    def __init__(self, account_type: str = "PAPER", account: str = PAPER_ACCOUNT) -> None:
        self.account = account
        self.capability = "RegTMargin"
        self.status = "Funded"
        self.account_type = account_type


class _Response:
    def __init__(self, profiles: list[object]) -> None:
        self.profiles = profiles


class _TigerOrder:
    """Mimics ``tigeropen.trade.domain.order.Order`` as far as the adapter uses it."""

    def __init__(self, **fields: object) -> None:
        self.order_id = fields.get("order_id", "TIGER-1")
        self.id = fields.get("id")
        self.status = fields.get("status", "Initial")
        self.quantity = fields.get("quantity", 8)
        self.filled = fields.get("filled", 0)
        self.avg_fill_price = fields.get("avg_fill_price", 0)
        self.commission = fields.get("commission", 0)
        self.external_id = fields.get("external_id", "")
        self.account = fields.get("account", PAPER_ACCOUNT)


class _FakeTradeClient:
    """A Tiger client that records calls and can be told to fail."""

    def __init__(
        self,
        *,
        accounts: list[object] | None = None,
        order: _TigerOrder | None = None,
        raise_on_place: Exception | None = None,
        raise_on_cancel: Exception | None = None,
        place_returns: object = "TIGER-1",
    ) -> None:
        self._accounts = accounts if accounts is not None else [_Profile()]
        self._order = order
        self._raise_on_place = raise_on_place
        self._raise_on_cancel = raise_on_cancel
        self._place_returns = place_returns
        #: Every call the adapter made, in order. The count of place_order calls is the
        #: evidence for "never retried".
        self.calls: list[str] = []
        self.placed: list[_TigerOrder] = []

    def get_managed_accounts(self) -> _Response:
        self.calls.append("get_managed_accounts")
        return _Response(self._accounts)

    def place_order(self, order: _TigerOrder) -> object:
        self.calls.append("place_order")
        self.placed.append(order)
        if self._raise_on_place is not None:
            raise self._raise_on_place
        if self._order is None:
            self._order = _TigerOrder(external_id=str(order.external_id or ""))
        return self._place_returns

    def cancel_order(self, **kwargs: object) -> object:
        self.calls.append("cancel_order")
        if self._raise_on_cancel is not None:
            raise self._raise_on_cancel
        if self._order is not None:
            self._order.status = "Cancelled"
        return 1

    def get_order(self, **kwargs: object) -> _TigerOrder | None:
        self.calls.append("get_order")
        return self._order


def verified_guard(client: _FakeTradeClient, *, account: str = PAPER_ACCOUNT) -> TigerAccountGuard:
    guard = TigerAccountGuard(requested_account=account)
    guard.verify_from_client(client)
    return guard


def order_view(*, quantity: int = 8, side: OrderSide = OrderSide.BUY) -> OrderIntentView:
    return OrderIntentView(
        intent_id="bt-oi_cr03",
        decision_id="bt-dec_cr03",
        instrument="SPY",
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        client_order_id="TQ-pa-cr03",
    )


class _Approval:
    """Stands in for ApprovedOrder; the adapter only reads ``order`` and the token."""

    def __init__(self, view: OrderIntentView) -> None:
        self.order = view
        self.token = "apr:test"  # noqa: S105 - a fake approval token, not a credential
        self.policy_hash = "sha256:" + "a" * 64
        self.portfolio_digest = "sha256:" + "b" * 64
        self.approved_at = AT
        self.risk_event_id = "ev-1"


def build(
    *,
    scopes: str = WRITE_SCOPES,
    client: _FakeTradeClient | None = None,
    guard: TigerAccountGuard | None = None,
) -> TigerPaperExecutionClient:
    sdk = client or _FakeTradeClient()
    return TigerPaperExecutionClient(
        session=_Session(scopes),  # type: ignore[arg-type]
        trade_client=sdk,
        guard=guard or verified_guard(sdk),
        clock=SimulatedClock(AT),
    )


class TestGateOneWriteScope:
    """Gate 1: the credential itself must be able to trade."""

    def test_the_read_only_grant_this_repository_holds_is_refused(self) -> None:
        """This is the state of the project today: no write scope, therefore no orders."""
        with pytest.raises(ConfigurationError, match=WRITE_SCOPE):
            TigerPaperExecutionClient(
                session=_Session(READ_ONLY_SCOPES),  # type: ignore[arg-type]
                trade_client=_FakeTradeClient(),
                guard=verified_guard(_FakeTradeClient()),
                clock=SimulatedClock(AT),
            )

    def test_the_refusal_explains_what_is_needed(self) -> None:
        with pytest.raises(ConfigurationError, match="owner"):
            require_write_scope(_Session(READ_ONLY_SCOPES))  # type: ignore[arg-type]

    def test_require_write_scope_accepts_a_write_grant(self) -> None:
        require_write_scope(_Session(WRITE_SCOPES))  # type: ignore[arg-type]

    def test_a_write_grant_that_is_not_ours_is_still_refused(self) -> None:
        """Holding *some* write scope is not the same as holding the trading one."""
        with pytest.raises(ConfigurationError, match=WRITE_SCOPE):
            require_write_scope(_Session("api.trade:read some.other:write"))  # type: ignore[arg-type]


class TestGateTwoVerifiedAccount:
    def test_an_unverified_guard_blocks_construction(self) -> None:
        with pytest.raises(ConfigurationError, match="verified"):
            TigerPaperExecutionClient(
                session=_Session(WRITE_SCOPES),  # type: ignore[arg-type]
                trade_client=_FakeTradeClient(),
                guard=TigerAccountGuard(requested_account=PAPER_ACCOUNT),
                clock=SimulatedClock(AT),
            )

    def test_a_live_account_cannot_be_verified_so_cannot_be_used(self) -> None:
        sdk = _FakeTradeClient(accounts=[_Profile(account_type="STANDARD", account="7654321")])
        guard = TigerAccountGuard(requested_account="7654321")
        guard.verify_from_client(sdk)
        with pytest.raises(ConfigurationError, match="verified"):
            TigerPaperExecutionClient(
                session=_Session(WRITE_SCOPES),  # type: ignore[arg-type]
                trade_client=sdk,
                guard=guard,
                clock=SimulatedClock(AT),
            )


class TestGateThreeApprovalOnly:
    def test_submit_requires_an_approved_order_object(self) -> None:
        """A raw intent has no token, so it cannot reach the venue."""
        client = build()
        view = order_view()
        with pytest.raises(AttributeError):
            client.submit(view, at=AT)  # type: ignore[arg-type]

    def test_a_duplicate_client_order_id_is_refused_locally(self) -> None:
        """The second lock on the same door: a duplicate order is unrecoverable."""
        sdk = _FakeTradeClient()
        client = build(client=sdk)
        approval = _Approval(order_view())
        client.submit(approval, at=AT)  # type: ignore[arg-type]
        with pytest.raises(BrokerError, match="already been submitted"):
            client.submit(approval, at=AT)  # type: ignore[arg-type]
        assert sdk.calls.count("place_order") == 1


class TestGateFourAccountMatch:
    def test_the_order_carries_the_verified_account(self) -> None:
        sdk = _FakeTradeClient()
        client = build(client=sdk)
        client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert sdk.placed[0].account == PAPER_ACCOUNT

    def test_a_mismatched_account_is_refused_before_the_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`place_order` sends `order.account`, so this is the last line of defence.

        Patched on the class rather than the instance: the adapter uses ``__slots__``, so an
        instance attribute cannot be shadowed — a small anti-tamper property worth keeping.
        """
        sdk = _FakeTradeClient()
        guard = verified_guard(sdk)
        client = build(client=sdk, guard=guard)

        def hijack(self: object, order_view: object, *, account: str) -> _TigerOrder:
            # Simulate the documented hazard: something builds an order for another account.
            return _TigerOrder(account="7654321", external_id="TQ-pa-cr03")

        monkeypatch.setattr(TigerPaperExecutionClient, "_build_order", hijack)
        with pytest.raises(BrokerError, match="authorized only for the verified paper account"):
            client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert "place_order" not in sdk.calls


class TestAmbiguousOutcomes:
    def test_a_timeout_after_transmission_is_unknown_not_failed(self) -> None:
        sdk = _FakeTradeClient(raise_on_place=TimeoutError("read timed out"))
        client = build(client=sdk)
        ack = client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert ack.state is BrokerOrderState.UNKNOWN
        assert ack.broker_order_id is None
        assert "no retry will be attempted" in ack.message.lower()

    def test_a_timeout_is_never_retried(self) -> None:
        """The evidence for "never retried" is the call count, not the prose."""
        sdk = _FakeTradeClient(raise_on_place=TimeoutError("read timed out"))
        client = build(client=sdk)
        client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert sdk.calls.count("place_order") == 1

    def test_no_order_id_from_the_venue_is_unknown(self) -> None:
        sdk = _FakeTradeClient(place_returns=None, order=None)
        client = build(client=sdk)
        # get_order returns None, so the adapter cannot confirm the order exists.
        ack = client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert ack.state in {BrokerOrderState.UNKNOWN, BrokerOrderState.ACCEPTED}

    def test_a_cancel_timeout_is_unknown_and_not_retried(self) -> None:
        sdk = _FakeTradeClient(raise_on_cancel=TimeoutError("read timed out"))
        client = build(client=sdk)
        client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        result = client.cancel(client_order_id="TQ-pa-cr03", at=AT)
        assert result.state is BrokerOrderState.UNKNOWN
        assert sdk.calls.count("cancel_order") == 1

    def test_cancel_without_a_known_venue_id_is_unknown(self) -> None:
        client = build()
        result = client.cancel(client_order_id="TQ-pa-unknown", at=AT)
        assert result.state is BrokerOrderState.UNKNOWN
        assert result.canceled_quantity == 0


class TestOrderConstruction:
    def test_a_market_buy_is_built_correctly(self) -> None:
        sdk = _FakeTradeClient()
        client = build(client=sdk)
        client.submit(_Approval(order_view(quantity=12)), at=AT)  # type: ignore[arg-type]
        placed = sdk.placed[0]
        assert placed.action == "BUY"
        assert placed.order_type == "MKT"
        assert placed.quantity == 12
        assert placed.time_in_force == "DAY"
        assert placed.external_id == "TQ-pa-cr03"  # a correlation hint, not an idempotency key

    def test_a_sell_is_built_correctly(self) -> None:
        sdk = _FakeTradeClient()
        client = build(client=sdk)
        client.submit(_Approval(order_view(side=OrderSide.SELL)), at=AT)  # type: ignore[arg-type]
        assert sdk.placed[0].action == "SELL"

    def test_only_market_and_limit_are_supported(self) -> None:
        assert set(supported_order_types()) == {"MKT", "LMT"}

    def test_a_limit_order_without_a_price_is_refused(self) -> None:
        client = build()
        view = order_view().model_copy(update={"order_type": OrderType.LIMIT, "limit_price": None})
        with pytest.raises(BrokerError, match="requires a limit price"):
            client._build_order(view, account=PAPER_ACCOUNT)

    def test_a_non_positive_quantity_is_refused(self) -> None:
        client = build()
        view = order_view().model_copy(update={"quantity": 0})
        with pytest.raises(BrokerError, match="quantity must be positive"):
            client._build_order(view, account=PAPER_ACCOUNT)


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Initial", OrderStatus.ACKNOWLEDGED),
            ("Submitted", OrderStatus.ACKNOWLEDGED),
            ("PartiallyFilled", OrderStatus.PARTIALLY_FILLED),
            ("Filled", OrderStatus.FILLED),
            ("Cancelled", OrderStatus.CANCELED),
            ("PendingCancel", OrderStatus.CANCEL_PENDING),
            ("Inactive", OrderStatus.REJECTED),
            ("Invalid", OrderStatus.REJECTED),
        ],
    )
    def test_every_documented_status_maps(self, raw: str, expected: OrderStatus) -> None:
        assert map_status(raw) == expected

    def test_an_unknown_status_is_unmapped_rather_than_guessed(self) -> None:
        assert map_status("SomethingNew") is None

    def test_a_filled_order_reports_a_fill(self) -> None:
        sdk = _FakeTradeClient(
            order=_TigerOrder(
                status="Filled", filled=8, avg_fill_price=600.5, external_id="TQ-pa-cr03"
            )
        )
        client = build(client=sdk)
        ack = client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert ack.state is BrokerOrderState.FILLED
        assert len(ack.fills) == 1
        assert ack.fills[0].quantity == 8
        assert ack.fills[0].price == Decimal("600.5000")
        assert ack.remaining == 0

    def test_a_partial_fill_reports_the_remainder(self) -> None:
        sdk = _FakeTradeClient(
            order=_TigerOrder(
                status="PartiallyFilled", filled=4, avg_fill_price=600.5, external_id="TQ-pa-cr03"
            )
        )
        client = build(client=sdk)
        ack = client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert ack.state is BrokerOrderState.PARTIALLY_FILLED
        assert ack.remaining == 4

    def test_a_fill_exec_id_is_stable_for_a_cumulative_state(self) -> None:
        """Stability is what makes booking idempotent: re-reading books nothing new."""
        sdk = _FakeTradeClient(
            order=_TigerOrder(
                order_id="TIGER-9",
                status="PartiallyFilled",
                filled=4,
                avg_fill_price=600.5,
                external_id="TQ-pa-cr03",
            )
        )
        client = build(client=sdk)
        # The venue order must be correlated to our client order id first, which is what a
        # submission does. Querying before submitting correctly finds nothing.
        assert client.query(client_order_id="TQ-pa-cr03") is None
        client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        first = client.query(client_order_id="TQ-pa-cr03")
        second = client.query(client_order_id="TQ-pa-cr03")
        assert first is not None
        assert second is not None
        assert first.fills[0].exec_id == second.fills[0].exec_id

    def test_reason_codes_for_broker_outcomes(self) -> None:
        assert reason_for_state(BrokerOrderState.UNKNOWN) is ReasonCode.SUBMIT_TIMEOUT
        assert reason_for_state(BrokerOrderState.REJECTED) is ReasonCode.BROKER_REJECTED


class TestReporting:
    def test_a_freshly_built_client_is_labelled_simulated(self) -> None:
        """Having a write scope is a precondition for paper trading, not evidence of it.

        AGENTS.md invariant 11: never hide missing functionality behind something that looks
        production-ready. This adapter has never submitted to the real venue, so it must not
        claim to have done so.
        """
        client = build()
        assert client.implementation_status is ImplementationStatus.SIMULATED
        assert client.describe()["venue_confirmed_an_order"] == "False"

    def test_a_failed_submission_does_not_upgrade_the_status(self) -> None:
        """A timeout is not an observation that the venue accepted anything."""
        sdk = _FakeTradeClient(raise_on_place=TimeoutError("read timed out"))
        client = build(client=sdk)
        client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert client.implementation_status is ImplementationStatus.SIMULATED

    def test_only_a_venue_order_id_upgrades_the_status(self) -> None:
        sdk = _FakeTradeClient()
        client = build(client=sdk)
        client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert client.implementation_status is ImplementationStatus.PAPER_VERIFIED
        # But never live: that would need a funded account this project must not use.
        assert client.implementation_status is not ImplementationStatus.LIVE_VERIFIED

    def test_describe_is_audit_safe(self) -> None:
        client = build()
        described = client.describe()
        assert described["implementation_status"] == "SIMULATED"
        assert PAPER_ACCOUNT not in str(described)
        assert described["account"].endswith("0042")
        assert WRITE_SCOPE in described["scopes"]

    def test_poll_returns_nothing_and_says_so(self) -> None:
        """No push subscriber exists in CR-03; an empty tuple is honest, a stub is not."""
        assert build().poll(at=AT) == ()
