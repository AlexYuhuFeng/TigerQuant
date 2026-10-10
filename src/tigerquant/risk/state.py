"""Risk state: halt, freeze and the actors allowed to change them.

The kill switch is sticky and human-clearable. An agent actor can neither engage nor
clear it in a way that opens risk: only the watchdog (and the owner) may halt, and only
the owner may clear. This is the mechanism behind acceptance gate 16's "risk
configuration cannot be changed through the AI decision API".
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from tigerquant.core.enums import ReasonCode

__all__ = ["Actor", "RiskState", "RiskStateTransition"]


class Actor(StrEnum):
    """Who is asking for a risk-state change."""

    #: The deterministic supervisor. May halt; may never clear a halt.
    WATCHDOG = "WATCHDOG"
    #: The human owner. The only actor that may clear a halt.
    OWNER = "OWNER"
    #: The AI decision path. May not change risk state at all.
    AGENT = "AGENT"
    #: The deterministic risk engine itself, for automatic freezes.
    RISK_ENGINE = "RISK_ENGINE"


class RiskStateTransition:
    """A recorded halt/freeze change."""

    __slots__ = ("actor", "at", "detail", "frozen", "halted", "reason_code")

    def __init__(
        self,
        *,
        at: dt.datetime,
        actor: Actor,
        halted: bool,
        frozen: bool,
        reason_code: ReasonCode,
        detail: str,
    ) -> None:
        self.at = at
        self.actor = actor
        self.halted = halted
        self.frozen = frozen
        self.reason_code = reason_code
        self.detail = detail


class RiskState:
    """Whether new exposure is currently permitted.

    ``halted`` blocks new orders *and* clears nothing else: cancellation and
    reconciliation keep running, because a halt must not strand live orders
    (ARCHITECTURE.md "Safety invariants").
    """

    __slots__ = ("_frozen", "_halted", "_history", "_reason_code")

    def __init__(self, *, halted: bool = False, frozen: bool = False) -> None:
        self._halted = halted
        self._frozen = frozen
        self._reason_code: ReasonCode | None = None
        self._history: list[RiskStateTransition] = []

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def reason_code(self) -> ReasonCode | None:
        return self._reason_code

    @property
    def history(self) -> tuple[RiskStateTransition, ...]:
        return tuple(self._history)

    def blocks_new_exposure(self) -> bool:
        return self._halted or self._frozen

    def halt(
        self,
        *,
        actor: Actor,
        at: dt.datetime,
        reason_code: ReasonCode,
        detail: str,
    ) -> bool:
        """Engage the halt. Returns ``False`` when already halted (idempotent)."""
        if actor is Actor.AGENT:
            msg = (
                "the AI decision path may not change risk state "
                "(AGENTS.md invariant 10, acceptance gate 16)"
            )
            raise PermissionError(msg)
        if self._halted:
            return False
        self._halted = True
        self._reason_code = reason_code
        self._history.append(
            RiskStateTransition(
                at=at,
                actor=actor,
                halted=True,
                frozen=self._frozen,
                reason_code=reason_code,
                detail=detail,
            )
        )
        return True

    def freeze(
        self,
        *,
        actor: Actor,
        at: dt.datetime,
        reason_code: ReasonCode,
        detail: str,
    ) -> bool:
        """Freeze new exposure without a full halt (used for unreconciled state)."""
        if actor is Actor.AGENT:
            msg = (
                "the AI decision path may not change risk state "
                "(AGENTS.md invariant 10, acceptance gate 16)"
            )
            raise PermissionError(msg)
        if self._frozen:
            return False
        self._frozen = True
        self._reason_code = reason_code
        self._history.append(
            RiskStateTransition(
                at=at,
                actor=actor,
                halted=self._halted,
                frozen=True,
                reason_code=reason_code,
                detail=detail,
            )
        )
        return True

    def clear(self, *, actor: Actor, at: dt.datetime, detail: str = "") -> bool:
        """Clear halt and freeze. Owner-only."""
        if actor is not Actor.OWNER:
            msg = (
                f"only the OWNER may clear a risk halt; {actor.value} attempted it "
                "(AGENTS.md invariant 10)"
            )
            raise PermissionError(msg)
        if not (self._halted or self._frozen):
            return False
        self._halted = False
        self._frozen = False
        self._reason_code = None
        self._history.append(
            RiskStateTransition(
                at=at,
                actor=actor,
                halted=False,
                frozen=False,
                reason_code=ReasonCode.RISK_HALTED,
                detail=detail or "cleared by owner",
            )
        )
        return True
