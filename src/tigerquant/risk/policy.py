"""Risk policy — immutable, versioned, human-approved data.

The policy is plain data with a content hash recorded in every risk outcome (ADR-0007
§4). Nothing at runtime can mutate it: ``model_config`` is frozen and the object is
loaded once when the worker starts. Acceptance gate 16 (no AI/dashboard path to change a
limit) is therefore a property of the type, not of a permission check.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field

from tigerquant.core.canonical import digest_of

__all__ = ["PortfolioView", "PositionView", "RiskPolicy", "TradingSessionView"]


class PositionView(Protocol):
    """What the gateway needs to know about one holding."""

    @property
    def quantity(self) -> int: ...


class PortfolioView(Protocol):
    """Read-only portfolio surface consumed by the risk gateway.

    A structural protocol rather than a concrete import keeps ``risk`` independent of
    the ledger implementation (ADR-0002 layering).
    """

    @property
    def cash(self) -> Decimal: ...

    def position(self, instrument: str) -> PositionView: ...

    def equity(self, marks: dict[str, Decimal]) -> Decimal: ...

    def gross_exposure(self, marks: dict[str, Decimal]) -> Decimal: ...

    def weight_of(self, instrument: str, marks: dict[str, Decimal]) -> Decimal: ...

    def digest(self) -> str: ...


class TradingSessionView(Protocol):
    """Exchange-calendar surface. CR-05 replaces the CR-01 weekday/time implementation."""

    def is_open(self, instant: object) -> bool: ...

    def is_within_approved_window(self, instant: object) -> bool: ...

    def is_halted(self, instrument: str, instant: object) -> bool: ...


class RiskPolicy(BaseModel):
    """The complete, immutable limit set for one deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Human-readable policy version; bumped by review, recorded in the journal.
    policy_version: Annotated[str, Field(min_length=1)]
    #: Version of the instrument universe this policy was approved against.
    universe_version: Annotated[str, Field(min_length=1)] = "unset"
    allowed_instruments: Annotated[tuple[str, ...], Field(min_length=1)]

    gross_exposure_fraction: Annotated[Decimal, Field(ge=0, le=1)]
    max_symbol_fraction: Annotated[Decimal, Field(ge=0, le=1)]
    max_order_fraction: Annotated[Decimal, Field(ge=0, le=1)]

    # -- session limits (CR-07) --------------------------------------------------
    #: Cumulative traded notional per session, as a fraction of opening equity. Bounds how
    #: much a strategy can churn even while every individual order is within its own cap.
    max_turnover_fraction: Annotated[Decimal, Field(ge=0)] = Decimal("2.0")
    #: Stop opening new exposure once the session has lost this fraction of its opening
    #: equity. One-sided: a gain is not a negative loss.
    max_daily_loss_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.02")
    #: Stop once equity has fallen this fraction below its session peak. Measured from the
    #: peak, so a losing streak cannot be magically reset by a partial recovery.
    max_drawdown_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.05")
    #: Submissions permitted in any rolling hour. Complements ``max_orders_per_cycle``,
    #: which only bounds a single pipeline cycle.
    max_orders_per_hour: Annotated[int, Field(ge=0)] = 30

    #: A mark older than this invalidates the decision that would use it.
    max_mark_age_seconds: Annotated[int, Field(ge=1)]
    #: A quoted price must be strictly positive; zero/negative is a data fault.
    min_mark_price: Annotated[Decimal, Field(gt=0)] = Decimal("0.01")

    allow_margin: bool = False
    allow_short: bool = False
    allow_options: bool = False

    max_orders_per_cycle: Annotated[int, Field(ge=0)] = 1

    def content_hash(self) -> str:
        """Digest recorded alongside every risk outcome."""
        return digest_of(self)

    def is_allowed(self, instrument: str) -> bool:
        return instrument in self.allowed_instruments

    def session_limits(self) -> tuple[Decimal, Decimal, Decimal, int]:
        """The four session limits, in a stable order for reporting."""
        return (
            self.max_turnover_fraction,
            self.max_daily_loss_fraction,
            self.max_drawdown_fraction,
            self.max_orders_per_hour,
        )
