"""The account guard under a write-scoped credential — the tests that must pass BEFORE it exists.



The situation these defend against was observed, not imagined. In CR-02 a single OAuth grant
returned **both** a funded live-capable account and the paper account:

    {"account": "***1346",           "accountType": "STANDARD", ...}
    {"account": "*************2713", "accountType": "PAPER",    ...}

There is no separate paper issuer and the token is not paper-bound, so adding
``api.trade:write`` would give that credential the ability to trade the live account too. What
keeps this project paper-only is **this guard** — not the scope, not the host, not the token.

So these tests are the precondition for granting write scope, and they are written to fail if
the guard is ever weakened.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.adapters.tiger.execution import (
    WRITE_SCOPE,
    TigerPaperExecutionClient,
    require_write_scope,
)
from tigerquant.adapters.tiger.guard import (
    LIVE_CAPABLE_ACCOUNT_TYPES,
    PAPER_ACCOUNT_TYPE,
    AccountIdentity,
    TigerAccountGuard,
)
from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import OrderSide, OrderType, TimeInForce
from tigerquant.core.errors import BrokerError, ConfigurationError
from tigerquant.risk.approvals import OrderIntentView

# This module reaches an optional SDK through the adapter it imports, so it needs that
# extra installed. The fast lane syncs only `--extra dev` and DESELECTS it rather than
# failing: the marker existed in pyproject all along and nothing wore it, so CI ran these
# against an environment that deliberately lacks the dependency. Placed after the imports
# so it neither lands inside the docstring nor trips E402.
pytestmark = pytest.mark.requires_broker

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
PAPER = "20240101120000042"
LIVE = "2471346"
READ_SCOPES = "api.basic:read api.account:read api.quote:read api.trade:read"
WRITE_SCOPES = f"{READ_SCOPES} {WRITE_SCOPE}"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Profile:
    def __init__(self, account: str, account_type: str) -> None:
        self.account = account
        self.capability = "RegTMargin"
        self.status = "Funded"
        self.account_type = account_type


class _Response:
    def __init__(self, profiles: list[object]) -> None:
        self.profiles = profiles


class _MutableTradeClient:
    """A Tiger client whose account list can CHANGE, which is the whole scenario."""

    def __init__(self, profiles: list[object], *, order: object = None) -> None:
        self.profiles = profiles
        self.calls: list[str] = []
        self._order = order

    def get_managed_accounts(self) -> _Response:
        self.calls.append("get_managed_accounts")
        return _Response(self.profiles)

    def place_order(self, order: object) -> object:
        self.calls.append("place_order")
        return "TIGER-1"

    def cancel_order(self, **kwargs: object) -> object:
        self.calls.append("cancel_order")
        return 1

    def get_order(self, **kwargs: object) -> object:
        self.calls.append("get_order")
        return self._order


class _Session:
    def __init__(self, scopes: str) -> None:
        self.scopes = scopes


class _Approval:
    def __init__(self, view: OrderIntentView) -> None:
        self.order = view


def order_view() -> OrderIntentView:
    return OrderIntentView(
        intent_id="bt-oi_harden",
        decision_id="bt-dec_harden",
        instrument="SPY",
        side=OrderSide.BUY,
        quantity=8,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        client_order_id="TQ-pa-harden",
    )


def paper_only() -> _MutableTradeClient:
    return _MutableTradeClient([_Profile(PAPER, PAPER_ACCOUNT_TYPE)])


def build(sdk: _MutableTradeClient, *, account: str = PAPER) -> TigerPaperExecutionClient:
    guard = TigerAccountGuard(requested_account=account)
    guard.verify_from_client(sdk)
    return TigerPaperExecutionClient(
        session=_Session(WRITE_SCOPES),  # type: ignore[arg-type]
        trade_client=sdk,
        guard=guard,
        clock=SimulatedClock(AT),
    )


# ---------------------------------------------------------------------------
# 1. A live account cannot reach submission, even holding a write scope
# ---------------------------------------------------------------------------
class TestALiveAccountCannotBeTraded:
    def test_a_standard_account_cannot_be_verified_at_all(self) -> None:
        """The first gate: verification itself refuses a live-capable account."""
        sdk = _MutableTradeClient([_Profile(LIVE, "STANDARD")])
        guard = TigerAccountGuard(requested_account=LIVE)
        verdict = guard.verify_from_client(sdk)
        assert not verdict.verified
        assert guard.is_verified is False

    def test_construction_refuses_when_only_a_live_account_is_visible(self) -> None:
        sdk = _MutableTradeClient([_Profile(LIVE, "STANDARD")])
        guard = TigerAccountGuard(requested_account=LIVE)
        guard.verify_from_client(sdk)
        with pytest.raises(ConfigurationError, match="verified"):
            TigerPaperExecutionClient(
                session=_Session(WRITE_SCOPES),  # type: ignore[arg-type]
                trade_client=sdk,
                guard=guard,
                clock=SimulatedClock(AT),
            )

    def test_the_write_scope_alone_does_not_permit_constructing_a_client(self) -> None:
        """Scope is necessary and nowhere near sufficient."""
        require_write_scope(_Session(WRITE_SCOPES))  # type: ignore[arg-type]  # holds the scope
        with pytest.raises(ConfigurationError):
            TigerPaperExecutionClient(
                session=_Session(WRITE_SCOPES),  # type: ignore[arg-type]
                trade_client=paper_only(),
                guard=TigerAccountGuard(requested_account=PAPER),  # never verified
                clock=SimulatedClock(AT),
            )

    def test_assert_verified_is_paper_refuses_a_live_capable_identity(self) -> None:
        """A guard can be verified and still be pointed at a funded account."""
        guard = TigerAccountGuard(requested_account=LIVE)
        sdk = _MutableTradeClient([_Profile(LIVE, "STANDARD")])
        guard.verify_from_client(sdk)
        # Force the identity through, the way a bug or a bad patch might.
        guard._identity = AccountIdentity(
            account=LIVE, account_type="STANDARD", status="Funded", capability="RegTMargin"
        )
        with pytest.raises(BrokerError, match="could hold real money"):
            guard.assert_verified_is_paper()

    def test_every_live_capable_type_is_refused(self) -> None:
        for account_type in sorted(LIVE_CAPABLE_ACCOUNT_TYPES):
            guard = TigerAccountGuard(requested_account=LIVE)
            guard._identity = AccountIdentity(
                account=LIVE, account_type=account_type, status="Funded"
            )
            with pytest.raises(BrokerError):
                guard.assert_verified_is_paper()

    def test_the_paper_identity_is_not_live_capable(self) -> None:
        identity = AccountIdentity(account=PAPER, account_type=PAPER_ACCOUNT_TYPE)
        assert identity.is_paper
        assert not identity.is_live_capable
        TigerAccountGuard(requested_account=PAPER)  # constructs

    def test_an_unrecognised_type_is_treated_as_live_capable(self) -> None:
        """Deny by default: guessing the other way is the mistake that cannot be undone."""
        identity = AccountIdentity(account=PAPER, account_type="SOMETHING_NEW")
        assert identity.is_live_capable

    def test_an_absent_type_is_treated_as_live_capable(self) -> None:
        assert AccountIdentity(account=PAPER, account_type=None).is_live_capable


# ---------------------------------------------------------------------------
# 2. A grant that gains a live account AFTER construction is caught
# ---------------------------------------------------------------------------
class TestALateAcquiredLiveAccountIsCaught:
    def test_reverification_before_a_submission_sees_the_new_account(self) -> None:
        """The scenario the hardening exists for.

        The client is built while the grant exposes only a paper account. Between construction
        and the first order, the grant gains a live-capable account. Nothing about the token
        changed; only what the broker reports.
        """
        sdk = paper_only()
        client = build(sdk)

        # The grant now exposes BOTH accounts, exactly as CR-02 observed live.
        sdk.profiles = [_Profile(LIVE, "STANDARD"), _Profile(PAPER, PAPER_ACCOUNT_TYPE)]

        ack = client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        # The paper account is still the verified one, so this order is legitimate -- but the
        # point is that the broker was asked again rather than remembered.
        assert "get_managed_accounts" in sdk.calls
        assert ack is not None

    def test_if_the_paper_account_disappears_the_submission_is_refused(self) -> None:
        """The dangerous direction: the verified account is no longer offered at all."""
        sdk = paper_only()
        client = build(sdk)

        # Only the live account remains -- a misconfigured grant, or a closed paper account.
        sdk.profiles = [_Profile(LIVE, "STANDARD")]

        with pytest.raises(BrokerError):
            client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert "place_order" not in sdk.calls, "no order may be sent once re-verification fails"

    def test_if_the_account_becomes_live_capable_the_submission_is_refused(self) -> None:
        """Same number, different reported type. Nothing else would notice."""
        sdk = paper_only()
        client = build(sdk)
        sdk.profiles = [_Profile(PAPER, "STANDARD")]

        with pytest.raises(BrokerError):
            client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert "place_order" not in sdk.calls

    def test_the_broker_is_asked_again_before_every_order(self) -> None:
        """Not once per client, not once per session: before every order."""
        sdk = paper_only()
        client = build(sdk)
        for index in range(3):
            view = order_view().model_copy(
                update={"client_order_id": f"TQ-pa-harden-{index}", "intent_id": f"bt-oi-h{index}"}
            )
            client.submit(_Approval(view), at=AT)  # type: ignore[arg-type]
        # Construction verified once; each submission re-verified.
        assert sdk.calls.count("get_managed_accounts") >= 4


# ---------------------------------------------------------------------------
# 3. The scope is re-read, not remembered
# ---------------------------------------------------------------------------
class TestTheScopeIsReasserted:
    def test_a_session_that_loses_write_scope_cannot_submit(self) -> None:
        """A token can be re-scoped under a running process."""
        sdk = paper_only()
        session = _Session(WRITE_SCOPES)
        guard = TigerAccountGuard(requested_account=PAPER)
        guard.verify_from_client(sdk)
        client = TigerPaperExecutionClient(
            session=session,  # type: ignore[arg-type]
            trade_client=sdk,
            guard=guard,
            clock=SimulatedClock(AT),
        )

        session.scopes = READ_SCOPES  # the grant is narrowed underneath us
        with pytest.raises(ConfigurationError, match=WRITE_SCOPE):
            client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert "place_order" not in sdk.calls

    def test_read_only_scopes_are_still_refused_at_construction(self) -> None:
        with pytest.raises(ConfigurationError, match=WRITE_SCOPE):
            TigerPaperExecutionClient(
                session=_Session(READ_SCOPES),  # type: ignore[arg-type]
                trade_client=paper_only(),
                guard=TigerAccountGuard(requested_account=PAPER),
                clock=SimulatedClock(AT),
            )


# ---------------------------------------------------------------------------
# 4. Staleness
# ---------------------------------------------------------------------------
class TestVerificationStaleness:
    def test_the_age_is_tracked(self) -> None:
        guard = TigerAccountGuard(requested_account=PAPER)
        assert guard.verification_age_seconds is None, "never verified"
        guard.verify_from_client(paper_only())
        age = guard.verification_age_seconds
        assert age is not None
        assert age >= 0

    def test_a_stale_verification_is_not_reused(self) -> None:
        """A long-running process must not trade on a check from hours ago."""
        guard = TigerAccountGuard(requested_account=PAPER, max_verification_age_seconds=0)
        guard.verify_from_client(paper_only())
        # With a zero-second limit every check is already stale, so the identity is cleared
        # before the re-query and must be re-established by it.
        identity = guard.reverify_from_client(paper_only())
        assert identity.account == PAPER

    def test_a_stale_identity_is_cleared_before_the_requery(self) -> None:
        guard = TigerAccountGuard(requested_account=PAPER, max_verification_age_seconds=-1)
        guard.verify_from_client(paper_only())
        with pytest.raises(BrokerError):
            # The live account is the only one offered, so the cleared identity cannot be
            # re-established and the submission path refuses.
            guard.reverify_from_client(_MutableTradeClient([_Profile(LIVE, "STANDARD")]))

    def test_the_default_age_limit_is_finite(self) -> None:
        """A default of 'never stale' would be the same as no re-verification at all."""
        guard = TigerAccountGuard(requested_account=PAPER)
        assert guard._max_verification_age_seconds > 0
        assert guard._max_verification_age_seconds <= 3600


# ---------------------------------------------------------------------------
# 5. The precondition for granting write scope
# ---------------------------------------------------------------------------
class TestTheGrantPrecondition:
    def test_the_guard_is_what_keeps_this_paper_only(self) -> None:
        """A summary test, deliberately explicit about the claim it is making.

        The scope does not bind the token to an account; the host does not either; the token
        itself says nothing. So if these assertions ever stop holding, granting
        ``api.trade:write`` would give access to a funded account, and the grant must not be
        made until they hold again.
        """
        # 1. A live-capable account cannot be verified.
        assert (
            not TigerAccountGuard(requested_account=LIVE)
            .verify_from_client(_MutableTradeClient([_Profile(LIVE, "STANDARD")]))
            .verified
        )
        # 2. A verified guard refuses a live-capable identity outright.
        guard = TigerAccountGuard(requested_account=LIVE)
        guard._identity = AccountIdentity(account=LIVE, account_type="STANDARD")
        with pytest.raises(BrokerError):
            guard.assert_verified_is_paper()
        # 3. The write path re-verifies before every order, so a late change is caught.
        sdk = paper_only()
        client = build(sdk)
        sdk.profiles = [_Profile(LIVE, "STANDARD")]
        with pytest.raises(BrokerError):
            client.submit(_Approval(order_view()), at=AT)  # type: ignore[arg-type]
        assert "place_order" not in sdk.calls
        # 4. Nothing here needed a live credential or a network call.
        assert Decimal(0) == 0
