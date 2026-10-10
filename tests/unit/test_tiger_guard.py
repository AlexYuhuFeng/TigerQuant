"""Tiger account guard tests.

The profile shapes here are the ones the live API actually returned on 2026-10-10 for an
OAuth grant whose holder had both a funded live-capable account and a paper account:

    {"account": "2471346",          "capability": "RegTMargin", "accountType": "STANDARD", "status": "Funded"}
    {"account": "20240101120000042","capability": "RegTMargin", "accountType": "PAPER",    "status": "Funded"}

Account numbers are replaced with synthetic values of the same *shape*: the live one is
short, the paper one is 17 digits. The guard's whole job is to tell those two apart, so the
tests must use both shapes — but not the owner's real identifiers.
"""

from __future__ import annotations

import pytest

from tigerquant.adapters.tiger.guard import (
    PAPER_ACCOUNT_MIN_DIGITS,
    AccountIdentity,
    TigerAccountGuard,
    mask_account,
    select_unique_paper_account,
    verify_paper_account,
)
from tigerquant.core.errors import BrokerError, ConfigurationError

# Shapes observed live: a short live-capable number, and a 17-digit paper number.
LIVE_ACCOUNT = "7654321"
PAPER_ACCOUNT = "20240101120000042"

LIVE_PROFILE = {
    "account": LIVE_ACCOUNT,
    "capability": "RegTMargin",
    "account_type": "STANDARD",
    "status": "Funded",
}
PAPER_PROFILE = {
    "account": PAPER_ACCOUNT,
    "capability": "RegTMargin",
    "account_type": "PAPER",
    "status": "Funded",
}


class _Profile:
    """Stands in for ``tigeropen.trade.domain.profile.AccountProfile``."""

    def __init__(self, **fields: str) -> None:
        self.account = fields.get("account")
        self.capability = fields.get("capability")
        self.status = fields.get("status")
        self.account_type = fields.get("account_type")


class _Response:
    def __init__(self, profiles: list[object]) -> None:
        self.profiles = profiles


class _FakeTradeClient:
    def __init__(self, profiles: list[object]) -> None:
        self._profiles = profiles
        self.calls = 0

    def get_managed_accounts(self) -> _Response:
        self.calls += 1
        return _Response(self._profiles)


class TestMasking:
    def test_only_the_last_four_digits_survive(self) -> None:
        assert mask_account(PAPER_ACCOUNT).endswith("0042")
        assert LIVE_ACCOUNT not in mask_account(PAPER_ACCOUNT)

    def test_short_values_are_fully_masked(self) -> None:
        assert mask_account("1234") == "****"

    def test_non_digits_are_dropped(self) -> None:
        assert mask_account("U1234567") == "***4567"


class TestVerifyPaperAccount:
    def test_paper_account_verifies(self) -> None:
        verdict = verify_paper_account([PAPER_PROFILE], requested_account=PAPER_ACCOUNT)
        assert verdict.verified
        assert verdict.identity is not None
        assert verdict.identity.account == PAPER_ACCOUNT

    def test_live_account_is_refused_even_when_asked_for_by_name(self) -> None:
        """The central safety property: a live account cannot be used, explicitly or not."""
        verdict = verify_paper_account(
            [PAPER_PROFILE, LIVE_PROFILE], requested_account=LIVE_ACCOUNT
        )
        assert not verdict.verified
        assert "not a Tiger PAPER account number" in verdict.detail
        assert LIVE_ACCOUNT not in verdict.detail  # masked in the message
        with pytest.raises(BrokerError):
            verdict.raise_if_refused()

    def test_configured_account_must_be_reported_by_the_broker(self) -> None:
        verdict = verify_paper_account([PAPER_PROFILE], requested_account="99999999999999999")
        assert not verdict.verified
        assert "not among the accounts the broker reports" in verdict.detail

    def test_no_accounts_is_a_refusal(self) -> None:
        verdict = verify_paper_account([], requested_account=None)
        assert not verdict.verified
        assert "no managed accounts" in verdict.detail

    def test_ambiguous_account_list_is_refused(self) -> None:
        verdict = verify_paper_account([PAPER_PROFILE, PAPER_PROFILE], requested_account=None)
        assert not verdict.verified
        assert "refusing to guess" in verdict.detail

    def test_single_account_needs_no_configuration(self) -> None:
        verdict = verify_paper_account([PAPER_PROFILE], requested_account=None)
        assert verdict.verified

    def test_paper_number_shape_is_enforced(self) -> None:
        short_paper = {"account": "12345", "account_type": "PAPER", "status": "Funded"}
        verdict = verify_paper_account([short_paper], requested_account=None)
        assert not verdict.verified

    def test_short_paper_numbers_can_be_allowed_explicitly(self) -> None:
        short_paper = {"account": "12345", "account_type": "PAPER", "status": "Funded"}
        verdict = verify_paper_account(
            [short_paper], requested_account=None, allow_short_paper_numbers=True
        )
        assert verdict.verified

    def test_unknown_account_type_is_refused_not_assumed(self) -> None:
        """Tiger renaming the field must fail loudly, not silently pass."""
        unknown = {"account": PAPER_ACCOUNT, "account_type": None, "status": "Funded"}
        verdict = verify_paper_account([unknown], requested_account=None)
        assert not verdict.verified
        assert "'PAPER'" in verdict.detail

    def test_account_type_is_compared_case_insensitively(self) -> None:
        variant = {"account": PAPER_ACCOUNT, "account_type": "paper", "status": "Funded"}
        assert verify_paper_account([variant], requested_account=None).verified

    @pytest.mark.parametrize("status", ["Closed", "Abandoned", "Rejected", "Unknown"])
    def test_unusable_statuses_are_refused(self, status: str) -> None:
        profile = {"account": PAPER_ACCOUNT, "account_type": "PAPER", "status": status}
        verdict = verify_paper_account([profile], requested_account=None)
        assert not verdict.verified

    def test_object_profiles_are_supported(self) -> None:
        verdict = verify_paper_account([_Profile(**PAPER_PROFILE)], requested_account=None)
        assert verdict.verified

    def test_profiles_without_an_account_are_ignored(self) -> None:
        verdict = verify_paper_account(
            [{"account": None, "account_type": "PAPER"}, PAPER_PROFILE], requested_account=None
        )
        assert verdict.verified
        assert len(verdict.observed) == 1


class TestSelectUniquePaperAccount:
    def test_picks_the_only_paper_account_among_a_live_one(self) -> None:
        verdict = select_unique_paper_account([LIVE_PROFILE, PAPER_PROFILE])
        assert verdict.verified
        assert verdict.identity is not None
        assert verdict.identity.account == PAPER_ACCOUNT

    def test_refuses_when_no_paper_account_exists(self) -> None:
        verdict = select_unique_paper_account([LIVE_PROFILE])
        assert not verdict.verified
        assert "no PAPER account" in verdict.detail

    def test_refuses_when_several_paper_accounts_exist(self) -> None:
        second = {**PAPER_PROFILE, "account": "20240101120000043"}
        verdict = select_unique_paper_account([PAPER_PROFILE, second])
        assert not verdict.verified
        assert "must configure which one" in verdict.detail

    def test_refuses_when_the_reported_type_contradicts_the_number_shape(self) -> None:
        """A 'PAPER' label on a short number is a disagreement, and a disagreement stops."""
        contradiction = {"account": "12345", "account_type": "PAPER", "status": "Funded"}
        verdict = select_unique_paper_account([contradiction])
        assert not verdict.verified
        assert "disagreement" in verdict.detail


class TestTigerAccountGuard:
    def test_verification_is_required_before_use(self) -> None:
        guard = TigerAccountGuard(requested_account=PAPER_ACCOUNT)
        assert not guard.is_verified
        with pytest.raises(ConfigurationError, match="no verified Tiger paper account"):
            guard.require_verified()

    def test_guard_freezes_the_verified_account(self) -> None:
        guard = TigerAccountGuard(requested_account=PAPER_ACCOUNT)
        verdict = guard.verify_from_client(_FakeTradeClient([LIVE_PROFILE, PAPER_PROFILE]))
        assert verdict.verified
        identity = guard.require_verified()
        assert identity.account == PAPER_ACCOUNT

    def test_a_later_failed_verification_invalidates_the_guard(self) -> None:
        guard = TigerAccountGuard(requested_account=PAPER_ACCOUNT)
        guard.verify_from_client(_FakeTradeClient([PAPER_PROFILE]))
        assert guard.is_verified
        guard.verify_from_client(_FakeTradeClient([LIVE_PROFILE]))
        assert not guard.is_verified

    def test_select_from_client_pins_the_broker_chosen_account(self) -> None:
        guard = TigerAccountGuard(requested_account=None)
        assert guard.select_from_client(_FakeTradeClient([LIVE_PROFILE, PAPER_PROFILE])).verified
        assert guard.requested_account == PAPER_ACCOUNT

    def test_assert_account_matches_closes_the_order_account_hazard(self) -> None:
        """`place_order` sends `order.account`, not `config.account`; this catches it."""
        guard = TigerAccountGuard(requested_account=PAPER_ACCOUNT)
        guard.verify_from_client(_FakeTradeClient([PAPER_PROFILE]))
        guard.assert_account_matches(PAPER_ACCOUNT)
        with pytest.raises(BrokerError, match="authorized only for the verified paper account"):
            guard.assert_account_matches(LIVE_ACCOUNT)

    def test_describe_is_audit_safe(self) -> None:
        guard = TigerAccountGuard(requested_account=PAPER_ACCOUNT)
        guard.verify_from_client(_FakeTradeClient([PAPER_PROFILE]))
        described = guard.describe()
        assert described["state"] == "verified-paper"
        assert PAPER_ACCOUNT not in str(described)
        assert "0042" in described["account"]

    def test_describe_before_verification_is_explicit(self) -> None:
        guard = TigerAccountGuard(requested_account=None)
        assert guard.describe()["state"] == "refused"

    def test_client_without_the_method_is_an_integration_error(self) -> None:
        from tigerquant.core.errors import IntegrationUnavailable

        guard = TigerAccountGuard(requested_account=None)
        with pytest.raises(IntegrationUnavailable):
            guard.verify_from_client(object())


class TestDocumentedConstants:
    def test_paper_length_matches_the_documented_rule(self) -> None:
        # AccountUtil.PAPER_ACCOUNT_DIGIT_LEN
        assert PAPER_ACCOUNT_MIN_DIGITS == 17

    def test_identity_record_never_carries_the_full_number(self) -> None:
        identity = AccountIdentity(
            account=PAPER_ACCOUNT, capability="RegTMargin", status="Funded", account_type="PAPER"
        )
        record = identity.as_record()
        assert record["account"] == mask_account(PAPER_ACCOUNT)
        assert PAPER_ACCOUNT not in "".join(str(value) for value in record.values())
