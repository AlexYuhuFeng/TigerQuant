"""Decision validation: structure, evidence, and deterministic recomputation.

The order is deliberate and non-negotiable (ADR-0006 §1):

1. **Structure** — the raw mapping must satisfy `schemas/trade_decision.schema.json`.
2. **Identity** — the ids the model echoed must be the ones we issued, and the snapshot
   must be the one it was shown.
3. **Evidence** — every cited id must resolve *inside that snapshot* (not merely exist in
   the store) and be fresh.
4. **Recomputation** — any arithmetic the model asserts is re-derived deterministically;
   disagreement is a rejection, not a correction.

Any failure produces :class:`DecisionValidationError`, which the pipeline converts into
`NO_TRADE` plus a `DecisionRejected` record. There is no path where a validation failure
becomes "trade anyway" (invariant 4).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tigerquant.core.enums import Action, ReasonCode
from tigerquant.core.errors import DecisionValidationError
from tigerquant.core.money import ZERO, q_weight, to_decimal
from tigerquant.decision.contract import TradeDecision, validate_against_schema
from tigerquant.evidence.staleness import StalenessPolicy

if TYPE_CHECKING:
    from tigerquant.evidence.snapshot import EvidenceSnapshot
    from tigerquant.evidence.store import EvidenceStore

__all__ = [
    "DecisionContext",
    "DecisionValidationError",
    "DecisionValidator",
    "RecomputationResult",
    "fresh_enough",
    "requires_evidence",
]


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Everything needed to validate one decision, all supplied by us."""

    snapshot: EvidenceSnapshot
    allowed_instruments: tuple[str, ...]
    expected_decision_id: str
    expected_model_version: str
    expected_prompt_version: str
    #: Current portfolio weight of the targeted instrument, for REDUCE/CLOSE consistency.
    current_weight: Decimal = ZERO
    #: Latest admissible mark for the targeted instrument, if available.
    mark_price: Decimal | None = None
    equity: Decimal | None = None
    #: The policy the snapshot was built with. Re-resolving with the same policy is a
    #: defence-in-depth check that the cited evidence is still admissible.
    staleness: StalenessPolicy | None = None

    def staleness_policy(self) -> StalenessPolicy:
        if self.staleness is not None:
            return self.staleness
        return StalenessPolicy.uniform(self.snapshot.max_age_seconds)


@dataclass(frozen=True, slots=True)
class RecomputationResult:
    """Deterministic re-derivation of a decision's numeric content."""

    ok: bool
    reason_code: ReasonCode | None
    detail: str
    implied_notional: Decimal | None

    @classmethod
    def passed(cls, *, implied_notional: Decimal | None = None) -> RecomputationResult:
        return cls(
            ok=True,
            reason_code=None,
            detail="numeric content consistent",
            implied_notional=implied_notional,
        )

    @classmethod
    def failed(cls, reason_code: ReasonCode, detail: str) -> RecomputationResult:
        return cls(ok=False, reason_code=reason_code, detail=detail, implied_notional=None)


class DecisionValidator:
    """Validates untrusted proposals. Stateless apart from the evidence store."""

    __slots__ = ("_schema", "_store")

    def __init__(self, store: EvidenceStore, *, schema: dict[str, Any] | None = None) -> None:
        self._store = store
        self._schema = schema

    def validate(self, raw: dict[str, Any], context: DecisionContext) -> TradeDecision:
        """Run every stage and return the typed decision, or raise."""
        validate_against_schema(raw, schema=self._schema)
        try:
            decision = TradeDecision.model_validate(raw)
        except Exception as exc:
            msg = f"decision failed typed validation: {exc}"
            raise DecisionValidationError(msg, ReasonCode.SCHEMA_INVALID, stage="typed") from exc

        self._check_identity(decision, context)
        self._check_semantics(decision, context)
        self._check_evidence(decision, context)
        self._check_recomputation(decision, context)
        return decision

    # -- stage 2: identity -------------------------------------------------------

    @staticmethod
    def _check_identity(decision: TradeDecision, context: DecisionContext) -> None:
        if decision.snapshot_id != context.snapshot.snapshot_id:
            msg = (
                f"decision cites snapshot {decision.snapshot_id!r} but was shown "
                f"{context.snapshot.snapshot_id!r}; a mismatched snapshot means the "
                "decision was not made against the evidence we control"
            )
            raise DecisionValidationError(msg, ReasonCode.SNAPSHOT_MISMATCH, stage="identity")
        if decision.decision_id != context.expected_decision_id:
            msg = (
                f"decision_id {decision.decision_id!r} does not match the id we issued "
                f"({context.expected_decision_id!r}); ids are allocated by the platform, "
                "not by the model"
            )
            raise DecisionValidationError(msg, ReasonCode.SEMANTIC_INVALID, stage="identity")
        if decision.model_version != context.expected_model_version:
            msg = (
                f"model_version {decision.model_version!r} does not match the resolved "
                f"provider release {context.expected_model_version!r}"
            )
            raise DecisionValidationError(msg, ReasonCode.MODEL_VERSION_UNPINNED, stage="identity")

    # -- stage 3: semantics ------------------------------------------------------

    @staticmethod
    def _check_semantics(decision: TradeDecision, context: DecisionContext) -> None:
        if decision.is_trade():
            missing = [
                name for name, value in decision.justification_fields().items() if not value.strip()
            ]
            if missing:
                msg = f"action {decision.action.value} requires a non-empty {', '.join(missing)}"
                raise DecisionValidationError(msg, ReasonCode.SEMANTIC_INVALID, stage="semantics")
            if decision.instrument is None:
                msg = f"action {decision.action.value} requires an instrument"
                raise DecisionValidationError(msg, ReasonCode.SEMANTIC_INVALID, stage="semantics")
            if decision.instrument not in context.allowed_instruments:
                msg = (
                    f"instrument {decision.instrument!r} is outside the approved universe "
                    f"{list(context.allowed_instruments)}"
                )
                raise DecisionValidationError(msg, ReasonCode.SYMBOL_NOT_ALLOWED, stage="semantics")

        if decision.action in {Action.REDUCE, Action.CLOSE} and decision.target_weight is None:
            msg = f"action {decision.action.value} requires an explicit target_weight"
            raise DecisionValidationError(msg, ReasonCode.SEMANTIC_INVALID, stage="semantics")

    # -- stage 4: evidence -------------------------------------------------------

    def _check_evidence(self, decision: TradeDecision, context: DecisionContext) -> None:
        if decision.requires_evidence() and not decision.evidence_ids:
            msg = "no evidence cited for an exposure-changing action (no evidence means NO_TRADE)"
            raise DecisionValidationError(msg, ReasonCode.EVIDENCE_UNRESOLVED, stage="evidence")

        absent = context.snapshot.resolve_absent(decision.evidence_ids)
        if absent:
            msg = (
                f"decision cites evidence {list(absent)} that is not part of snapshot "
                f"{context.snapshot.snapshot_id}; unresolvable evidence is treated as fabricated"
            )
            raise DecisionValidationError(msg, ReasonCode.EVIDENCE_UNRESOLVED, stage="evidence")

        report = self._store.resolve(
            decision.evidence_ids,
            as_of=context.snapshot.as_of,
            staleness=context.staleness_policy(),
        )
        if not report.ok:
            reason = report.reason_code() or ReasonCode.EVIDENCE_UNRESOLVED
            raise DecisionValidationError(
                f"evidence resolution failed: {report.summary()}", reason, stage="evidence"
            )
        if decision.evidence_ids and not report.usable_facts:
            # Evidence was cited, everything resolved, but only injection-flagged content
            # remains. Flagged content is stored truthfully; it cannot justify a trade.
            # (Citing nothing at all is handled by the requires-evidence check above, so a
            # HOLD or NO_TRADE with no evidence is not caught here.)
            msg = (
                "every cited document is prompt-injection-flagged; flagged content cannot "
                "admit a trade (no evidence means NO_TRADE)"
            )
            raise DecisionValidationError(msg, ReasonCode.SNAPSHOT_INCOMPLETE, stage="evidence")

    # -- stage 5: recomputation --------------------------------------------------

    def _check_recomputation(self, decision: TradeDecision, context: DecisionContext) -> None:
        result = self.recompute(decision, context)
        if not result.ok:
            raise DecisionValidationError(
                result.detail,
                result.reason_code or ReasonCode.NUMERIC_DISAGREEMENT,
                stage="recomputation",
            )

    @staticmethod
    def recompute(decision: TradeDecision, context: DecisionContext) -> RecomputationResult:
        """Re-derive the numbers a decision implies.

        The model is never asked for a quantity, a price, a notional or a P&L; those are
        recomputed by deterministic services (invariant 5). What *is* checked here is
        that the action and the stated target weight do not contradict the portfolio the
        decision claims to act on, which is the offline form of acceptance gate 10.
        """
        target = decision.target_weight
        current = q_weight(to_decimal(context.current_weight))

        if target is None:
            if decision.action in {Action.REDUCE, Action.CLOSE}:
                return RecomputationResult.failed(
                    ReasonCode.NUMERIC_DISAGREEMENT,
                    f"{decision.action.value} without a target weight cannot be sized",
                )
            return RecomputationResult.passed()

        stated = q_weight(to_decimal(target))

        if decision.action is Action.CLOSE and stated != ZERO:
            return RecomputationResult.failed(
                ReasonCode.NUMERIC_DISAGREEMENT,
                f"CLOSE with target_weight {stated} is self-contradictory; a close means 0",
            )

        if decision.action is Action.REDUCE and stated >= current:
            return RecomputationResult.failed(
                ReasonCode.NUMERIC_DISAGREEMENT,
                f"REDUCE from {current} to {stated} does not reduce exposure",
            )

        implied_notional: Decimal | None = None
        if context.equity is not None and context.mark_price is not None:
            implied_notional = to_decimal(context.equity) * stated

        return RecomputationResult.passed(implied_notional=implied_notional)


def requires_evidence(action: Action) -> bool:
    """Whether an action may proceed with no evidence at all."""
    return action in {Action.SET_TARGET, Action.REDUCE, Action.CLOSE}


def fresh_enough(available_at: dt.datetime, as_of: dt.datetime, max_age_seconds: int) -> bool:
    """Small shared helper used by tests and the evidence resolver."""
    return (as_of - available_at).total_seconds() <= max_age_seconds
