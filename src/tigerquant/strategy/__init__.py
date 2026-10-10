"""Strategies. Everything here is deterministic; the model-backed path arrives in CR-06."""

from __future__ import annotations

from tigerquant.strategy.base import CandidateDecision, Strategy, StrategyContext
from tigerquant.strategy.reference import REFERENCE_MODEL_ID, MomentumReferenceStrategy

__all__ = [
    "REFERENCE_MODEL_ID",
    "CandidateDecision",
    "MomentumReferenceStrategy",
    "Strategy",
    "StrategyContext",
]
