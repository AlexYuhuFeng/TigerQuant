"""Reference momentum strategy — deterministic, rule-based, NOT an LLM.

This exists so the CR-01 slice exercises the *entire* production path (evidence →
snapshot → decision contract → sizing → risk → execution) without a network dependency.
It is labelled ``SIMULATED`` everywhere it surfaces. It is not a trading recommendation
and carries no claim of profitability; it is a test instrument with a predictable signal.

Rule (long-only, one instrument at a time):
  momentum_bps = (close_now / close_lookback_ago - 1) * 10_000
  * momentum_bps >  entry_threshold_bps and current weight < target_weight  -> SET_TARGET
  * momentum_bps < -exit_threshold_bps and currently held                  -> CLOSE
  * otherwise                                                              -> HOLD
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.enums import Action
from tigerquant.core.errors import MissingMarketData
from tigerquant.core.money import ZERO, q_weight
from tigerquant.evidence.facts import fact_id_for
from tigerquant.strategy.base import CandidateDecision, StrategyContext

if TYPE_CHECKING:
    from tigerquant.market.fixtures import StrategyFixture

__all__ = ["MomentumReferenceStrategy"]

#: Version of the rule set. Bump on any change to the logic or its parameters.
STRATEGY_VERSION = "reference-momentum-1.0.0"

#: Resolved model version string the reference strategy reports. Deliberately explicit
#: that this is not a model: the journal must never imply an LLM produced this decision.
REFERENCE_MODEL_ID = "reference-momentum-rule-based"


class MomentumReferenceStrategy:
    """Deterministic momentum rule over the as-of view."""

    __slots__ = ("_params",)

    def __init__(self, params: StrategyFixture) -> None:
        self._params = params

    @property
    def name(self) -> str:
        return "reference-momentum"

    @property
    def version(self) -> str:
        return STRATEGY_VERSION

    @property
    def model_version(self) -> str:
        return REFERENCE_MODEL_ID

    def evaluate(self, context: StrategyContext) -> CandidateDecision:
        """Evaluate every instrument in the universe and return the strongest signal."""
        best: CandidateDecision | None = None
        best_magnitude = ZERO

        for symbol in context.universe:
            candidate = self._evaluate_symbol(symbol, context)
            magnitude = abs(candidate.confidence)
            if candidate.action is not Action.HOLD and magnitude > best_magnitude:
                best = candidate
                best_magnitude = magnitude

        if best is not None:
            return best

        return CandidateDecision(
            action=Action.HOLD,
            instrument=None,
            target_weight=None,
            thesis="no instrument in the approved universe exceeded the entry or exit threshold",
            counter_thesis="momentum is a lagging signal and can reverse without warning",
            invalidation="a threshold breach in the opposite direction",
            evidence_ids=(),
            uncertainties=("momentum rules can whipsaw in range-bound markets",),
            rationale="no signal",
        )

    def _evaluate_symbol(self, symbol: str, context: StrategyContext) -> CandidateDecision:
        lookback = self._params.lookback
        try:
            bars = context.view.require_history(symbol, lookback + 1)
        except MissingMarketData:
            return CandidateDecision(
                action=Action.HOLD,
                instrument=symbol,
                thesis=f"insufficient history for {symbol} to compute a {lookback}-bar momentum",
                counter_thesis="a shorter window would be noisier, not more informative",
                invalidation="more bars becoming available",
                evidence_ids=(),
                rationale="insufficient history",
            )

        oldest, newest = bars[0], bars[-1]
        momentum_bps = q_weight((newest.close / oldest.close - Decimal(1)) * Decimal(10_000))
        evidence = tuple(
            fact_id_for(
                category="price_bar",
                instrument=bar.instrument,
                source_id=bar.source_id,
                event_time=bar.occurred_at,
                content_hash=bar.content_hash,
            )
            for bar in bars
        )
        entry = Decimal(self._params.entry_threshold_bps)
        exit_threshold = Decimal(self._params.exit_threshold_bps)
        target_weight = Decimal(self._params.target_weight)
        rationale = (
            f"{symbol} {lookback}-bar momentum {momentum_bps}bps "
            f"(entry>{entry}, exit<-{exit_threshold})"
        )

        if momentum_bps > entry:
            return CandidateDecision(
                action=Action.SET_TARGET,
                instrument=symbol,
                target_weight=target_weight,
                horizon=self._params.horizon,
                thesis=(
                    f"{symbol} rose {momentum_bps}bps over the last {lookback} bars, above the "
                    f"{entry}bps entry threshold, so the rule targets a "
                    f"{target_weight} portfolio weight."
                ),
                counter_thesis=(
                    "Short-horizon momentum can be noise; the move may already be exhausted by "
                    "the time the order fills."
                ),
                invalidation=(
                    f"a close more than {exit_threshold}bps below the entry bar's close, or a "
                    "risk-gate denial"
                ),
                evidence_ids=evidence,
                uncertainties=(
                    "synthetic fixture data; no news, filings or liquidity analysis",
                    "the rule has no view on valuation",
                ),
                rationale=rationale,
                confidence=momentum_bps,
            )

        if momentum_bps < -exit_threshold:
            return CandidateDecision(
                action=Action.CLOSE,
                instrument=symbol,
                target_weight=ZERO,
                horizon="immediate",
                thesis=(
                    f"{symbol} fell {momentum_bps}bps over the last {lookback} bars, beyond the "
                    f"{exit_threshold}bps exit threshold, so the rule exits the position."
                ),
                counter_thesis="the decline may be a temporary drawdown rather than a trend change.",
                invalidation=f"a close more than {entry}bps above the entry bar's close",
                evidence_ids=evidence,
                uncertainties=("the exit is rule-driven, with no fundamental reassessment",),
                rationale=rationale,
                confidence=-momentum_bps,
            )

        return CandidateDecision(
            action=Action.HOLD,
            instrument=symbol,
            thesis=f"{symbol} momentum {momentum_bps}bps is inside the thresholds",
            counter_thesis="holding is still an exposure decision",
            invalidation="a threshold breach",
            evidence_ids=evidence,
            rationale=rationale,
            confidence=momentum_bps,
        )
