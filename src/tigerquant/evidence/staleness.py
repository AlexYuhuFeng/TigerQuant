"""Per-category staleness policy.

"Stale" is not one number. A price bar is unusable after seconds-to-minutes; a filing is
usable for months; a broker position snapshot is unusable almost immediately. A single
global ``max_age_seconds`` therefore either blocks legitimate filing-based reasoning or
admits minutes-old prices, so freshness is declared per fact category.

An unknown category falls back to ``default_seconds``, which must be the *most*
conservative value a deployment is willing to accept — a new category that nobody
configured should be treated as perishable, not as durable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from tigerquant.evidence.facts import Fact

__all__ = ["DEFAULT_STALENESS", "StalenessPolicy"]


class StalenessPolicy(BaseModel):
    """Freshness horizons, in seconds, keyed by fact category."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_seconds: Annotated[int, Field(ge=1)]
    per_category: dict[str, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_conservative_default(self) -> StalenessPolicy:
        # The default must not be looser than the loosest explicit category, otherwise a
        # newly-introduced category silently gets a longer life than intended.
        loosest = max(self.per_category.values(), default=self.default_seconds)
        if self.default_seconds > loosest:
            msg = (
                f"default_seconds ({self.default_seconds}) is looser than the loosest "
                f"configured category ({loosest}); the default must be conservative"
            )
            raise ValueError(msg)
        return self

    @classmethod
    def uniform(cls, seconds: int) -> StalenessPolicy:
        """One horizon for every category. Used by the CR-01 slice."""
        return cls(default_seconds=seconds, per_category={})

    def max_age_seconds(self, category: str) -> int:
        """Freshness horizon for ``category``, falling back to the conservative default."""
        return self.per_category.get(category, self.default_seconds)

    def for_fact(self, fact: Fact) -> int:
        return self.max_age_seconds(fact.category)

    def categories(self) -> tuple[str, ...]:
        return tuple(sorted(self.per_category))


def _coerce(policy: StalenessPolicy | int) -> StalenessPolicy:
    if isinstance(policy, bool):  # bool is an int subclass; never a duration
        msg = "staleness must be a StalenessPolicy or an int number of seconds"
        raise TypeError(msg)
    if isinstance(policy, int):
        return StalenessPolicy.uniform(policy)
    return policy


#: Horizons for the categories CR-05 ingests. Values are engineering choices, not
#: trading advice, and an operator may tighten them per deployment.
DEFAULT_STALENESS = StalenessPolicy(
    default_seconds=60,
    per_category={
        "price_bar": 900,  # 15 minutes: a decision on a stale bar is not a decision
        "quote": 60,  # a quote is perishable
        "position_snapshot": 30,
        "calendar_session": 86_400,
        "news_claim": 3_600,
        "filing_claim": 2_592_000,  # 30 days: a 10-K or 10-Q describes a period
        "filing_event": 604_800,  # 7 days: an 8-K or Form 4 announces a moment
        "derived_feature": 900,
    },
)
