"""Decision-plane events: evidence snapshots, proposals and rejections."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from tigerquant.core.enums import Action, ReasonCode, SnapshotCompleteness
from tigerquant.events.base import AwareUTC, EventBase, NonEmptyStr, PositiveDecimal

__all__ = ["DecisionProposed", "DecisionRejected", "SnapshotCreated"]


class SnapshotCreated(EventBase):
    """A frozen, point-in-time evidence snapshot (ADR-0005 §4)."""

    event_type: Literal["SnapshotCreated"] = "SnapshotCreated"

    snapshot_id: NonEmptyStr
    as_of: AwareUTC
    fact_ids: tuple[str, ...]
    completeness: SnapshotCompleteness
    #: Digest over the ordered fact ids and their contents.
    digest: NonEmptyStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    max_fact_age_seconds: int = Field(ge=0)
    notes: tuple[str, ...] = ()


class DecisionProposed(EventBase):
    """A candidate trade decision, already validated, recorded before sizing/risk.

    This is the *validated* form. The raw model output is recorded separately as an
    alert/diagnostic when it fails validation.
    """

    event_type: Literal["DecisionProposed"] = "DecisionProposed"

    decision_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    #: Resolved provider release, not the configured alias (ADR-0009 §2).
    model_version: NonEmptyStr
    prompt_version: NonEmptyStr
    action: Action
    instrument: str | None = None
    target_weight: Decimal | None = Field(default=None, ge=0, le=1)
    horizon: NonEmptyStr
    thesis: str
    counter_thesis: str
    invalidation: str
    evidence_ids: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    #: Deterministic recomputation outcome (ADR-0006 §3).
    recomputed_notional: PositiveDecimal | None = None

    @model_validator(mode="after")
    def _check_action_consistency(self) -> DecisionProposed:
        if self.action is Action.SET_TARGET:
            if self.instrument is None or self.target_weight is None:
                msg = "SET_TARGET requires instrument and target_weight"
                raise ValueError(msg)
        return self


class DecisionRejected(EventBase):
    """A decision (or raw model output) that failed validation. Always recorded."""

    event_type: Literal["DecisionRejected"] = "DecisionRejected"

    decision_id: str | None = None
    snapshot_id: str | None = None
    stage: NonEmptyStr
    reason_code: ReasonCode
    detail: str
    #: Hash of the offending raw output. The raw text itself is kept out of the audit
    #: journal when it may contain licensed or injected content.
    raw_output_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
