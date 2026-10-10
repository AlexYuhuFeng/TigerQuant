"""Decision providers: what produces a raw proposal.

The provider interface is deliberately narrow. A provider receives a frozen, validated
:class:`DecisionRequest` and returns a raw mapping; it cannot size, price, submit or even
read the ledger. CR-06 plugs an LLM provider in here without touching the pipeline.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from tigerquant.core.errors import TigerQuantError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from tigerquant.evidence.snapshot import EvidenceSnapshot
    from tigerquant.market.asof import PointInTimeView
    from tigerquant.strategy.base import Strategy

__all__ = [
    "DecisionProvider",
    "DecisionRequest",
    "ScriptedDecisionProvider",
    "StrategyDecisionProvider",
]


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """The complete, frozen input to a decision.

    Note what is *not* here: no broker handle, no tool registry, no credentials, and no
    ability to change risk configuration (ADR-0009 §1).
    """

    snapshot: EvidenceSnapshot
    as_of: dt.datetime
    decision_id: str
    model_version: str
    prompt_version: str
    universe: tuple[str, ...]
    equity: Decimal
    marks: Mapping[str, Decimal]
    current_weights: Mapping[str, Decimal] = field(default_factory=dict)
    #: The as-of view a rule-based provider may read. A model provider ignores it and
    #: works from :meth:`to_prompt_payload` instead.
    view: PointInTimeView | None = None

    def to_prompt_payload(self) -> dict[str, Any]:
        """The JSON-serialisable view handed to a model provider in CR-06."""
        return {
            "snapshot_id": self.snapshot.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "decision_id": self.decision_id,
            "universe": list(self.universe),
            "equity": str(self.equity),
            "marks": {key: str(value) for key, value in sorted(self.marks.items())},
            "current_weights": {
                key: str(value) for key, value in sorted(self.current_weights.items())
            },
            "evidence": {
                "fact_ids": list(self.snapshot.fact_ids),
                "completeness": self.snapshot.completeness.value,
                "reasons": list(self.snapshot.reasons),
            },
            "instructions": (
                "Return one JSON object matching the trade decision schema. "
                "Cite only fact ids listed in evidence.fact_ids. "
                "Do not state quantities, prices, notionals or costs — they are recomputed."
            ),
        }


class DecisionProvider(Protocol):
    """Anything that can turn a :class:`DecisionRequest` into a raw proposal mapping."""

    @property
    def model_version(self) -> str: ...

    @property
    def prompt_version(self) -> str: ...

    def propose(self, request: DecisionRequest) -> dict[str, Any]: ...


class ScriptedDecisionProvider:
    """Returns a fixed mapping. Used to drive validation tests deterministically."""

    __slots__ = ("_model_version", "_prompt_version", "_raw", "_transform")

    def __init__(
        self,
        raw: dict[str, Any] | None = None,
        *,
        model_version: str = "scripted-test-provider",
        prompt_version: str = "scripted-1",
        transform: Callable[[DecisionRequest], Mapping[str, Any]] | None = None,
    ) -> None:
        self._raw = raw or {}
        self._model_version = model_version
        self._prompt_version = prompt_version
        self._transform = transform

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def propose(self, request: DecisionRequest) -> dict[str, Any]:
        if self._transform is not None:
            return dict(self._transform(request))
        raw = dict(self._raw)
        raw.setdefault("snapshot_id", request.snapshot.snapshot_id)
        raw.setdefault("decision_id", request.decision_id)
        raw.setdefault("model_version", self._model_version)
        return raw


class StrategyDecisionProvider:
    """Adapts a deterministic strategy to the provider interface."""

    __slots__ = ("_model_version", "_prompt_version", "_strategy")

    def __init__(self, strategy: Strategy, *, model_version: str, prompt_version: str) -> None:
        self._strategy = strategy
        self._model_version = model_version
        self._prompt_version = prompt_version

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def propose(self, request: DecisionRequest) -> dict[str, Any]:
        from tigerquant.strategy.base import StrategyContext

        view = request.view
        if view is None:
            msg = (
                "StrategyDecisionProvider requires request.view; build the DecisionRequest "
                "with the point-in-time view attached"
            )
            raise TigerQuantError(msg)
        candidate = self._strategy.evaluate(
            StrategyContext(
                as_of=request.as_of,
                view=view,
                equity=request.equity,
                marks=dict(request.marks),
                universe=request.universe,
                max_fact_age_seconds=request.snapshot.max_age_seconds,
            )
        )
        return candidate.to_raw(
            snapshot_id=request.snapshot.snapshot_id,
            model_version=self._model_version,
            decision_id=request.decision_id,
        )
