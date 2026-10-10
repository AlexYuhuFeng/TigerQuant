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
from tigerquant.evidence.staleness import StalenessPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tigerquant.evidence.staleness import StalenessPolicy
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
    """Builds snapshots from a store, with a per-category staleness policy."""

    __slots__ = ("_staleness", "_store")

    def __init__(
        self,
        store: EvidenceStore,
        *,
        staleness: StalenessPolicy | int | None = None,
        max_age_seconds: int | None = None,
    ) -> None:
        if staleness is None and max_age_seconds is None:
            msg = "SnapshotBuilder requires either 'staleness' or 'max_age_seconds'"
            raise ValueError(msg)
        if staleness is not None and max_age_seconds is not None:
            msg = "pass either 'staleness' or 'max_age_seconds', not both"
            raise ValueError(msg)

        # Normalise to exactly one type before storing, so nothing downstream has to
        # consider the "an int was passed" case.
        if isinstance(staleness, StalenessPolicy):
            policy = staleness
        elif isinstance(staleness, int) and not isinstance(staleness, bool):
            policy = StalenessPolicy.uniform(staleness)
        elif max_age_seconds is not None:
            policy = StalenessPolicy.uniform(max_age_seconds)
        else:  # pragma: no cover - guarded by the checks above
            msg = "SnapshotBuilder requires a StalenessPolicy or a number of seconds"
            raise ValueError(msg)

        self._store = store
        self._staleness: StalenessPolicy = policy

    @property
    def staleness(self) -> StalenessPolicy:
        return self._staleness

    @property
    def max_age_seconds(self) -> int:
        """The conservative default horizon, for the snapshot record."""
        return self._staleness.default_seconds

    def build(
        self,
        *,
        as_of: dt.datetime,
        fact_ids: Iterable[str],
        notes: Iterable[str] = (),
    ) -> EvidenceSnapshot:
        """Resolve ``fact_ids`` at ``as_of`` and freeze the outcome."""
        ordered = tuple(dict.fromkeys(fact_ids))
        report = self._store.resolve(ordered, as_of=as_of, staleness=self._staleness)

        if report.missing or report.stale:
            completeness = SnapshotCompleteness.INCOMPLETE
        elif report.contradictions:
            completeness = SnapshotCompleteness.CONTRADICTORY
        elif not ordered:
            completeness = SnapshotCompleteness.INCOMPLETE
        elif not report.usable_facts:
            # Everything cited resolved, but only injection-flagged content remains.
            # Flagged content is stored truthfully and still cannot admit a trade.
            completeness = SnapshotCompleteness.INCOMPLETE
        else:
            completeness = SnapshotCompleteness.COMPLETE

        reasons: list[str] = []
        if not ordered:
            reasons.append("no evidence cited")
        reasons.extend(f"unresolved evidence id: {fact_id}" for fact_id in report.missing)
        reasons.extend(f"stale evidence id: {fact_id}" for fact_id in report.stale)
        reasons.extend(f"contradicted evidence id: {fact_id}" for fact_id in report.contradictions)
        if ordered and report.ok and not report.usable_facts:
            reasons.append(
                "every cited document is prompt-injection-flagged; flagged content cannot "
                "admit a trade"
            )

        # The digest covers the resolved *effective* content, not just the ids, so two
        # snapshots with the same ids but different content can never share an id.
        material = {
            "as_of": as_of,
            "staleness": self._staleness,
            "facts": [
                {
                    "fact_id": res.effective.fact_id if res.effective else None,
                    "cited_as": fact_id,
                    "digest": res.effective.digest() if res.effective else None,
                }
                for fact_id, res in report.resolutions.items()
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
            max_age_seconds=self._staleness.default_seconds,
            reasons=tuple(reasons),
            notes=tuple(notes),
        )
