"""Frozen evidence snapshots.

A snapshot is what the decision model actually sees: an ordered, digest-pinned list of
fact ids at one ``as_of`` instant, with a completeness verdict. A model output that cites
an id outside its own snapshot is rejected, and an incomplete or contradictory snapshot
forces abstention rather than a confident guess (ADR-0005 §4-5).
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tigerquant.core.canonical import digest_of
from tigerquant.core.enums import SnapshotCompleteness
from tigerquant.events.base import AwareUTC, NonEmptyStr

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tigerquant.evidence.store import EvidenceStore

__all__ = ["EvidenceSnapshot", "SnapshotBuilder"]


class EvidenceSnapshot(BaseModel):
    """An immutable, point-in-time evidence set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: NonEmptyStr
    as_of: AwareUTC
    fact_ids: tuple[str, ...]
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    completeness: SnapshotCompleteness
    max_age_seconds: int = Field(ge=1)
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_sorted_unique(self) -> EvidenceSnapshot:
        if len(set(self.fact_ids)) != len(self.fact_ids):
            msg = "snapshot fact_ids must be unique"
            raise ValueError(msg)
        return self

    @property
    def is_usable(self) -> bool:
        """Only a COMPLETE snapshot with at least one fact may support a trade."""
        return self.completeness is SnapshotCompleteness.COMPLETE and bool(self.fact_ids)

    def contains(self, fact_id: str) -> bool:
        return fact_id in self.fact_ids

    def resolve_absent(self, cited: Iterable[str]) -> tuple[str, ...]:
        """Cited ids that are not part of this snapshot."""
        return tuple(fact_id for fact_id in cited if fact_id not in self.fact_ids)


class SnapshotBuilder:
    """Builds snapshots from a store, with an explicit staleness horizon."""

    __slots__ = ("_max_age_seconds", "_store")

    def __init__(self, store: EvidenceStore, *, max_age_seconds: int) -> None:
        if max_age_seconds < 1:
            msg = "max_age_seconds must be positive"
            raise ValueError(msg)
        self._store = store
        self._max_age_seconds = max_age_seconds

    @property
    def max_age_seconds(self) -> int:
        return self._max_age_seconds

    def build(
        self,
        *,
        as_of: dt.datetime,
        fact_ids: Iterable[str],
        notes: Iterable[str] = (),
    ) -> EvidenceSnapshot:
        """Resolve ``fact_ids`` at ``as_of`` and freeze the outcome."""
        ordered = tuple(dict.fromkeys(fact_ids))
        report = self._store.resolve(ordered, as_of=as_of, max_age_seconds=self._max_age_seconds)

        if report.missing or report.stale:
            completeness = SnapshotCompleteness.INCOMPLETE
        elif report.contradictions:
            completeness = SnapshotCompleteness.CONTRADICTORY
        elif not ordered:
            completeness = SnapshotCompleteness.INCOMPLETE
        else:
            completeness = SnapshotCompleteness.COMPLETE

        reasons: list[str] = []
        if not ordered:
            reasons.append("no evidence cited")
        reasons.extend(f"unresolved evidence id: {fact_id}" for fact_id in report.missing)
        reasons.extend(f"stale evidence id: {fact_id}" for fact_id in report.stale)
        reasons.extend(f"contradicted evidence id: {fact_id}" for fact_id in report.contradictions)

        # The digest covers the resolved content, not just the ids, so two snapshots
        # with the same ids but different fact content can never share an id.
        material = {
            "as_of": as_of,
            "max_age_seconds": self._max_age_seconds,
            "facts": [
                {"fact_id": res.fact.fact_id, "digest": res.fact.digest()}
                for res in report.resolutions.values()
                if res.fact is not None
            ],
            "completeness": completeness,
        }
        digest = digest_of(material)
        return EvidenceSnapshot(
            snapshot_id=f"snap:{digest.removeprefix('sha256:')[:24]}",
            as_of=as_of,
            fact_ids=ordered,
            digest=digest,
            completeness=completeness,
            max_age_seconds=self._max_age_seconds,
            reasons=tuple(reasons),
            notes=tuple(notes),
        )
