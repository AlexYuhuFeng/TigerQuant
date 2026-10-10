"""Risk-plane events. The gateway is the only authority that can emit ``RiskEvaluated``
with ``outcome=ALLOW`` (ADR-0007)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from tigerquant.core.enums import ReasonCode, RiskOutcomeKind, Severity
from tigerquant.events.base import EventBase, NonEmptyStr, NonNegativeInt

__all__ = ["AlertRaised", "RiskEvaluated", "RiskStateChanged"]


class RiskEvaluated(EventBase):
    """The result of evaluating one order intent against the risk policy."""

    event_type: Literal["RiskEvaluated"] = "RiskEvaluated"

    intent_id: NonEmptyStr
    outcome: RiskOutcomeKind
    reason_codes: tuple[ReasonCode, ...]
    #: Content hash of the policy that produced this outcome (ADR-0007 §4).
    policy_hash: NonEmptyStr
    #: Digest of the portfolio snapshot the decision was made against.
    portfolio_digest: NonEmptyStr
    checks_run: tuple[str, ...]
    detail: str = ""

    def is_allowed(self) -> bool:
        return self.outcome is RiskOutcomeKind.ALLOW


class RiskStateChanged(EventBase):
    """Halt / freeze transitions. Sticky across restarts until a human clears them."""

    event_type: Literal["RiskStateChanged"] = "RiskStateChanged"

    halted: bool
    frozen: bool
    reason_code: ReasonCode
    detail: str
    #: Identity that requested the change. Only the watchdog or the owner may halt.
    actor: NonEmptyStr
    #: ``True`` when a human must clear this state.
    requires_human_clear: bool = True


class AlertRaised(EventBase):
    """An operator-visible alert. Messages are redacted before they get here."""

    event_type: Literal["AlertRaised"] = "AlertRaised"

    severity: Severity
    code: ReasonCode
    message: str
    #: How many times this alert has fired, for false-alert measurement (CR-09).
    occurrences: NonNegativeInt = 1
    context: dict[str, Decimal | int | str | bool | None] = Field(default_factory=dict)
