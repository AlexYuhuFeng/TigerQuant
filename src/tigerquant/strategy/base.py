"""Strategy contract.

A strategy sees only a point-in-time view, a portfolio summary and the approved
universe. It returns a *candidate* decision that must survive validation; it cannot
size, cannot price, and cannot reach a broker.

The reference strategy in this package is deterministic and rule-based. It is **not** an
LLM and it is labelled ``SIMULATED``. The model-backed decision path arrives in CR-06
behind the same interface, so nothing downstream changes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from tigerquant.core.enums import Action

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tigerquant.market.asof import PointInTimeView

__all__ = ["CandidateDecision", "Strategy", "StrategyContext"]


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy is allowed to see. Note the absence of any write surface."""

    as_of: dt.datetime
    view: PointInTimeView
    equity: Decimal
    marks: Mapping[str, Decimal]
    universe: tuple[str, ...]
    max_fact_age_seconds: int


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """An unvalidated proposal. It has no power until the validator accepts it."""

    action: Action
    instrument: str | None = None
    target_weight: Decimal | None = None
    horizon: str = "5 trading days"
    thesis: str = ""
    counter_thesis: str = ""
    invalidation: str = ""
    evidence_ids: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = field(default=())
    #: Human-readable provenance of the rule that fired; recorded in the journal.
    rationale: str = ""
    confidence: Decimal = Decimal(0)

    def to_raw(
        self, *, snapshot_id: str, model_version: str, decision_id: str
    ) -> dict[str, object]:
        """Render the mapping that the schema validator consumes.

        This is the same shape a model would have to produce, so the reference strategy
        exercises exactly the production validation path.
        """
        return {
            "snapshot_id": snapshot_id,
            "model_version": model_version,
            "action": self.action.value,
            "instrument": self.instrument,
            # A JSON *number*, not a string: the repository schema declares
            # ``target_weight`` as a number, and a string would be a schema violation.
            # Decimal is an exact ``numbers.Number``, so no binary float is involved.
            "target_weight": self.target_weight,
            "horizon": self.horizon,
            "thesis": self.thesis,
            "counter_thesis": self.counter_thesis,
            "invalidation": self.invalidation,
            "evidence_ids": list(self.evidence_ids),
            "uncertainties": list(self.uncertainties),
            "decision_id": decision_id,
        }


class Strategy(Protocol):
    """Deterministic, side-effect-free decision producer."""

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def evaluate(self, context: StrategyContext) -> CandidateDecision: ...
