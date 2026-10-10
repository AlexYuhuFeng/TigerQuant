"""Broker-reported account identity guard.

Why a guard and not a config flag
---------------------------------
Under OAuth 2.0 the *account*, not the configuration, decides whether a call reaches a
paper or a funded account. The authorization server is a single production issuer, and
`tigeropen` will happily let an `Order` object built for one account be submitted through
a client configured for another (`place_order` sends ``order.account``, not
``config.account``). The published source also shows that ``config.account``'s setter only
ever sets ``is_paper = True`` and never back to ``False``, so `is_paper` can go stale.

Everything is therefore verified against the broker's own response, on every use, and the
default answer is refusal:

1. the account must appear in ``get_managed_accounts()``;
2. ``AccountUtil.is_paper_account`` must be true — Tiger's *only* documented paper
   predicate (all digits, length ≥ 17); an unknown or missing account type is a refusal,
   never a pass;
3. the account number used for account-scoped calls must equal the verified one.

This is ADR-0003 §3 in executable form, and it is the control that makes the paper/live
distinction real rather than cosmetic. It cannot be relaxed by configuration: there is no
option that turns it off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tigerquant.core.enums import ReasonCode, Severity
from tigerquant.core.errors import BrokerError, ConfigurationError, IntegrationUnavailable

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "AccountIdentity",
    "AccountVerdict",
    "TigerAccountGuard",
    "mask_account",
    "select_unique_paper_account",
    "verify_paper_account",
]

#: Tiger's documented paper-account length rule (`AccountUtil.PAPER_ACCOUNT_DIGIT_LEN`).
PAPER_ACCOUNT_MIN_DIGITS = 17


def mask_account(account: str) -> str:
    """Render an account number for logs and reports without disclosing it.

    Account numbers are personal identifiers. Reports and the audit journal use this form
    so a decision can be tied to an account without the number leaking into exports.
    """
    digits = "".join(char for char in account if char.isdigit())
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """One account as reported by the broker."""

    account: str
    capability: str | None = None
    status: str | None = None
    account_type: str | None = None

    def masked(self) -> str:
        return mask_account(self.account)

    def as_record(self) -> dict[str, str | None]:
        """Audit-safe rendering: the masked account plus the reported attributes."""
        return {
            "account": self.masked(),
            "capability": self.capability,
            "status": self.status,
            "account_type": self.account_type,
        }


@dataclass(frozen=True, slots=True)
class AccountVerdict:
    """Outcome of verifying the configured account.

    ``verified`` is ``True`` only when the broker itself reported a paper account matching
    the requested one. Every other outcome is a refusal with a machine-readable reason.
    """

    verified: bool
    reason_code: ReasonCode
    detail: str
    severity: Severity
    identity: AccountIdentity | None = None
    observed: tuple[AccountIdentity, ...] = field(default=())

    def raise_if_refused(self) -> AccountIdentity:
        if not self.verified or self.identity is None:
            msg = f"Tiger account verification refused: {self.detail}"
            raise BrokerError(msg)
        return self.identity


def select_unique_paper_account(profiles: Sequence[Any]) -> AccountVerdict:
    """Pick the single PAPER account from a broker response, or refuse.

    This exists so that a deployment never has to hard-code an account number in a
    configuration file, and so that the selection is still unambiguous: exactly one PAPER
    account must be present. Zero is a refusal (nothing to use) and more than one is a
    refusal (a human must say which), because choosing arbitrarily between two accounts is
    precisely the class of mistake this project cannot afford.
    """
    observed: list[AccountIdentity] = []
    for profile in profiles:
        if isinstance(profile, dict):
            get = profile.get
        else:
            get = lambda name, p=profile: getattr(p, name, None)  # noqa: E731
        account = get("account")
        if isinstance(account, str) and account:
            observed.append(
                AccountIdentity(
                    account=account,
                    capability=get("capability"),
                    status=get("status"),
                    account_type=get("account_type"),
                )
            )

    paper = [item for item in observed if (item.account_type or "").strip().upper() == "PAPER"]
    if not paper:
        return AccountVerdict(
            verified=False,
            reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
            detail=(
                "the broker reports no PAPER account for this authorization; this project "
                "operates paper-only, so there is nothing it may use"
            ),
            severity=Severity.CRITICAL,
            observed=tuple(observed),
        )
    if len(paper) > 1:
        return AccountVerdict(
            verified=False,
            reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
            detail=(
                f"the broker reports {len(paper)} PAPER accounts; a human must configure "
                "which one to use rather than have the system choose"
            ),
            severity=Severity.CRITICAL,
            observed=tuple(observed),
        )
    target = paper[0]
    if not _looks_like_a_paper_account(target.account):
        return AccountVerdict(
            verified=False,
            reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
            detail=(
                f"the account reported as PAPER ({target.masked()}) does not match Tiger's "
                f"documented paper-number shape (all digits, at least "
                f"{PAPER_ACCOUNT_MIN_DIGITS} characters); refusing on the disagreement"
            ),
            severity=Severity.CRITICAL,
            identity=target,
            observed=tuple(observed),
        )
    return AccountVerdict(
        verified=True,
        reason_code=ReasonCode.SYMBOL_NOT_ALLOWED,  # unused on success
        detail=(
            f"selected the unique PAPER account {target.masked()} "
            f"(capability={target.capability}, status={target.status})"
        ),
        severity=Severity.INFO,
        identity=target,
        observed=tuple(observed),
    )


def _looks_like_a_paper_account(account: str) -> bool:
    """Tiger's documented paper predicate: all digits, length ≥ 17."""
    return account.isdigit() and len(account) >= PAPER_ACCOUNT_MIN_DIGITS


def verify_paper_account(
    profiles: Sequence[Any],
    *,
    requested_account: str | None,
    allow_short_paper_numbers: bool = False,
) -> AccountVerdict:
    """Decide whether ``requested_account`` is a usable Tiger paper account.

    Pure function over the broker's response, so it is fully testable offline with
    synthetic profiles — which is what `tests/fault/test_tiger_guard.py` does.

    Parameters
    ----------
    profiles:
        Objects (or mappings) with ``account``, ``capability``, ``status`` and
        ``account_type`` attributes, as returned by ``get_managed_accounts()``.
    requested_account:
        The account this process intends to use. ``None`` means "the only account", which
        is accepted only when the broker reports exactly one.
    allow_short_paper_numbers:
        Escape hatch for a non-US paper account whose number is shorter than 17 digits.
        Defaults to ``False``; a caller that sets it takes explicit responsibility.
    """
    observed: list[AccountIdentity] = []
    for profile in profiles:
        if isinstance(profile, dict):
            get = profile.get
        else:
            get = lambda name, p=profile: getattr(p, name, None)  # noqa: E731
        account = get("account")
        if not isinstance(account, str) or not account:
            continue
        observed.append(
            AccountIdentity(
                account=account,
                capability=get("capability"),
                status=get("status"),
                account_type=get("account_type"),
            )
        )

    if not observed:
        return AccountVerdict(
            verified=False,
            reason_code=ReasonCode.EVIDENCE_UNRESOLVED,
            detail=(
                "the broker reported no managed accounts; nothing may be read or written "
                "until an account is visible"
            ),
            severity=Severity.CRITICAL,
        )

    if requested_account is None:
        if len(observed) != 1:
            return AccountVerdict(
                verified=False,
                reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
                detail=(
                    f"no account was configured and the broker reports {len(observed)} "
                    "accounts; refusing to guess which one to use"
                ),
                severity=Severity.CRITICAL,
                observed=tuple(observed),
            )
        target = observed[0]
    else:
        matches = [item for item in observed if item.account == requested_account]
        if not matches:
            return AccountVerdict(
                verified=False,
                reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
                detail=(
                    "the configured account is not among the accounts the broker reports "
                    "for this authorization; refusing to proceed"
                ),
                severity=Severity.CRITICAL,
                observed=tuple(observed),
            )
        target = matches[0]

    if not allow_short_paper_numbers and not _looks_like_a_paper_account(target.account):
        return AccountVerdict(
            verified=False,
            reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
            detail=(
                f"account {target.masked()} is not a Tiger PAPER account number (all-digit, "
                f"at least {PAPER_ACCOUNT_MIN_DIGITS} characters). This project operates "
                "paper-only; a live account is refused here, not later."
            ),
            severity=Severity.CRITICAL,
            identity=target,
            observed=tuple(observed),
        )

    # The broker's own account type is the authoritative signal. Observed live on
    # 2026-10-10: a funded live-capable account reports accountType 'STANDARD' while a
    # simulated account reports 'PAPER', in the same response, for the same OAuth grant.
    # A missing or unrecognised type is a refusal: treating "unknown" as pass would make
    # the guard useless exactly when the API changes.
    reported_type = (target.account_type or "").strip().upper()
    if reported_type != "PAPER":
        return AccountVerdict(
            verified=False,
            reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
            detail=(
                f"account {target.masked()} is reported with account type "
                f"{target.account_type!r}, not 'PAPER'. An unknown or non-paper type is a "
                "refusal, never a pass."
            ),
            severity=Severity.CRITICAL,
            identity=target,
            observed=tuple(observed),
        )

    # 'Closed'/'Abandoned' accounts are visible but not usable.
    if target.status is not None and target.status.lower() in {
        "closed",
        "abandoned",
        "rejected",
        "unknown",
    }:
        return AccountVerdict(
            verified=False,
            reason_code=ReasonCode.MODE_CAPABILITY_ABSENT,
            detail=(
                f"account {target.masked()} is reported with status {target.status!r}, which "
                "is not a usable account state"
            ),
            severity=Severity.WARNING,
            identity=target,
            observed=tuple(observed),
        )

    return AccountVerdict(
        verified=True,
        reason_code=ReasonCode.SYMBOL_NOT_ALLOWED,  # unused on success; see AccountVerdict
        detail=(
            f"verified paper account {target.masked()} "
            f"(capability={target.capability}, status={target.status}, "
            f"account_type={target.account_type})"
        ),
        severity=Severity.INFO,
        identity=target,
        observed=tuple(observed),
    )


class TigerAccountGuard:
    """Holds a verified account for the lifetime of one process.

    Once verification has succeeded the account is frozen: a later `get_managed_accounts`
    result cannot silently change which account the process trades. Callers must ask the
    guard for the account rather than reading configuration, so a config edit cannot
    redirect a running process at a funded account.
    """

    __slots__ = ("_identity", "_requested", "_verdict")

    def __init__(self, *, requested_account: str | None) -> None:
        self._requested = requested_account
        self._identity: AccountIdentity | None = None
        self._verdict: AccountVerdict | None = None

    @property
    def requested_account(self) -> str | None:
        return self._requested

    @property
    def is_verified(self) -> bool:
        return self._identity is not None

    @property
    def verdict(self) -> AccountVerdict | None:
        return self._verdict

    def select_from_client(self, trade_client: Any) -> AccountVerdict:
        """Pick and verify the unique PAPER account from the broker's own list.

        The holdout is what makes this safe rather than convenient: the account number is
        taken from the broker's response, never from configuration, and the guard refuses
        unless exactly one PAPER account exists and it matches Tiger's documented shape.
        """
        response = trade_client.get_managed_accounts()
        profiles = getattr(response, "profiles", None)
        if profiles is None and isinstance(response, (list, tuple)):
            profiles = response
        verdict = select_unique_paper_account(list(profiles or []))
        self._verdict = verdict
        self._identity = verdict.identity if verdict.verified else None
        if verdict.verified and verdict.identity is not None:
            self._requested = verdict.identity.account
        return verdict

    def verify_from_client(
        self, trade_client: Any, *, allow_short_paper_numbers: bool = False
    ) -> AccountVerdict:
        """Call ``get_managed_accounts()`` and verify the result. Read-only; no order path."""
        if not hasattr(trade_client, "get_managed_accounts"):
            raise IntegrationUnavailable("Tiger TradeClient", "broker")
        response = trade_client.get_managed_accounts()
        profiles = getattr(response, "profiles", None)
        if profiles is None and isinstance(response, (list, tuple)):
            profiles = response
        verdict = verify_paper_account(
            list(profiles or []),
            requested_account=self._requested,
            allow_short_paper_numbers=allow_short_paper_numbers,
        )
        self._verdict = verdict
        if verdict.verified:
            self._identity = verdict.identity
        else:
            # A previously verified account is invalidated by a failed re-verification.
            self._identity = None
        return verdict

    def require_verified(self) -> AccountIdentity:
        """Return the verified account, or refuse."""
        if self._identity is None:
            verdict = self._verdict
            detail = verdict.detail if verdict is not None else "verification has not been run"
            raise ConfigurationError(f"no verified Tiger paper account: {detail}")
        return self._identity

    def assert_account_matches(self, account: str) -> None:
        """Assert that a value about to be sent to the broker is the verified account.

        Used at the single broker-write call site (CR-03) to close the
        ``order.account != config.account`` hazard documented in
        ``docs/integrations/tigeropen_api_notes.md`` §8.
        """
        identity = self.require_verified()
        if account != identity.account:
            msg = (
                f"refusing to use account {mask_account(account)}: this process is "
                f"authorized only for the verified paper account {identity.masked()}"
            )
            raise BrokerError(msg)

    def describe(self) -> dict[str, str]:
        """Audit-safe description: masked account and reported attributes only."""
        if self._identity is None:
            return {"account": "(unverified)", "state": "refused"}
        record = self._identity.as_record()
        record["state"] = "verified-paper"
        return {key: "" if value is None else str(value) for key, value in record.items()}
