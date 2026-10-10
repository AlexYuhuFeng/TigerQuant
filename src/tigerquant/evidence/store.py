"""Point-in-time evidence store (interface frozen in CR-01; durable store in CR-05).

The only read API is ``as_of``-bounded. There is deliberately no ``get_latest()``: a
caller that wants "the newest fact" must say *as of when*, which is what makes
acceptance gate 12 (no future revisions in replay) a property of the interface rather
than of reviewer discipline.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import ReasonCode
from tigerquant.core.errors import EvidenceError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from tigerquant.evidence.facts import Fact

__all__ = ["EvidenceStore", "Resolution", "ResolutionReport"]


class Resolution:
    """Per-fact resolution result."""

    __slots__ = ("fact", "reason_code", "status")

    def __init__(self, status: str, fact: Fact | None, reason_code: ReasonCode | None) -> None:
        self.status = status
        self.fact = fact
        self.reason_code = reason_code

    def __repr__(self) -> str:
        return f"Resolution(status={self.status!r}, fact={None if self.fact is None else self.fact.fact_id})"


class ResolutionReport:
    """Aggregate outcome of resolving a set of cited evidence ids."""

    __slots__ = ("as_of", "contradictions", "missing", "resolutions", "stale")

    def __init__(
        self,
        *,
        as_of: dt.datetime,
        resolutions: dict[str, Resolution],
        missing: tuple[str, ...],
        stale: tuple[str, ...],
        contradictions: tuple[str, ...],
    ) -> None:
        self.as_of = as_of
        self.resolutions = resolutions
        self.missing = missing
        self.stale = stale
        self.contradictions = contradictions

    @property
    def resolved_ids(self) -> tuple[str, ...]:
        return tuple(
            fact_id for fact_id, res in self.resolutions.items() if res.status == "RESOLVED"
        )

    @property
    def ok(self) -> bool:
        return not (self.missing or self.stale or self.contradictions)

    def reason_code(self) -> ReasonCode | None:
        """The single reason code that should force abstention, if any."""
        if self.missing:
            return ReasonCode.EVIDENCE_UNRESOLVED
        if self.stale:
            return ReasonCode.EVIDENCE_STALE
        if self.contradictions:
            return ReasonCode.EVIDENCE_CONTRADICTORY
        return None

    def summary(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append(f"unresolved={list(self.missing)}")
        if self.stale:
            parts.append(f"stale={list(self.stale)}")
        if self.contradictions:
            parts.append(f"contradictions={list(self.contradictions)}")
        return "; ".join(parts) if parts else "all evidence resolved"


class EvidenceStore:
    """Append-only, in-memory fact store with as-of reads.

    Facts are immutable and content-addressed; adding the same ``fact_id`` twice is a
    no-op, which makes ingestion idempotent (acceptance gate 7).
    """

    __slots__ = ("_facts",)

    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self) -> Iterator[Fact]:
        return iter(self._facts.values())

    def add(self, fact: Fact) -> bool:
        """Add ``fact``; return ``False`` when it was already present."""
        if fact.fact_id in self._facts:
            return False
        self._facts[fact.fact_id] = fact
        return True

    def extend(self, facts: Iterable[Fact]) -> int:
        """Add many facts; return how many were new."""
        return sum(1 for fact in facts if self.add(fact))

    def get(self, fact_id: str) -> Fact:
        """Fetch by id regardless of time. Raises when unknown."""
        try:
            return self._facts[fact_id]
        except KeyError as exc:
            msg = f"evidence id {fact_id!r} does not exist in the store"
            raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED) from exc

    def as_of(self, instant: dt.datetime, *, category: str | None = None) -> tuple[Fact, ...]:
        """Facts legitimately available at ``instant``, in insertion order."""
        moment = ensure_utc(instant)
        return tuple(
            fact
            for fact in self._facts.values()
            if fact.available_at <= moment and (category is None or fact.category == category)
        )

    def resolve(
        self,
        fact_ids: Iterable[str],
        *,
        as_of: dt.datetime,
        max_age_seconds: int,
    ) -> ResolutionReport:
        """Resolve cited evidence ids against the point-in-time view.

        Failure modes, all of which force abstention (invariant 4):

        * ``missing`` — the id does not exist, or existed only *after* ``as_of``
          (a model citing something it could not have seen is treated as fabrication).
        * ``stale`` — older than ``max_age_seconds`` or past ``valid_until``.
        * ``contradictions`` — two available facts of the same
          ``(instrument, category)`` disagree on ``value``.
        """
        moment = ensure_utc(as_of)
        resolutions: dict[str, Resolution] = {}
        missing: list[str] = []
        stale: list[str] = []
        seen: set[str] = set()

        for fact_id in fact_ids:
            if fact_id in seen:
                continue
            seen.add(fact_id)
            fact = self._facts.get(fact_id)
            if fact is None or not fact.is_known_at(moment):
                resolutions[fact_id] = Resolution("MISSING", None, ReasonCode.EVIDENCE_UNRESOLVED)
                missing.append(fact_id)
                continue
            age = (moment - fact.available_at).total_seconds()
            if fact.is_stale(moment) or age > max_age_seconds:
                resolutions[fact_id] = Resolution("STALE", fact, ReasonCode.EVIDENCE_STALE)
                stale.append(fact_id)
                continue
            resolutions[fact_id] = Resolution("RESOLVED", fact, None)

        contradictions = self._find_contradictions(
            [res.fact for res in resolutions.values() if res.fact is not None], moment
        )
        return ResolutionReport(
            as_of=moment,
            resolutions=resolutions,
            missing=tuple(missing),
            stale=tuple(stale),
            contradictions=contradictions,
        )

    def _find_contradictions(self, facts: list[Fact], as_of: dt.datetime) -> tuple[str, ...]:
        """Find facts contradicted by another available fact.

        Contradiction is scoped to the same ``(instrument, category, event_time)``: two
        sources disagreeing about the same observation. Consecutive price bars have
        different values by construction and are emphatically **not** contradictions.

        Two cases are detected:

        1. two cited facts that disagree with each other — citing both sides of a conflict
           is itself a contradiction, not a resolution;
        2. a cited fact that disagrees with another fact that was available at ``as_of``
           but was not cited.
        """
        if not facts:
            return ()
        cited = {fact.fact_id for fact in facts}
        conflicts: list[str] = []

        groups: dict[tuple[str, str, dt.datetime], list[Fact]] = {}
        for fact in facts:
            if fact.instrument is None or fact.value() is None:
                continue
            groups.setdefault((fact.instrument, fact.category, fact.event_time), []).append(fact)
        for group in groups.values():
            if len(group) < 2:
                continue
            if (
                len({member.value() for member in group}) > 1
                and len({member.source_id for member in group}) > 1
            ):
                conflicts.extend(member.fact_id for member in group)

        for fact in facts:
            if fact.instrument is None or fact.value() is None:
                continue
            for other in self._facts.values():
                if other.fact_id == fact.fact_id or other.fact_id in cited:
                    continue
                if (
                    other.instrument == fact.instrument
                    and other.category == fact.category
                    and other.event_time == fact.event_time
                    and other.source_id != fact.source_id
                    and other.value() is not None
                    and other.value() != fact.value()
                    and other.is_known_at(as_of)
                    and not other.is_stale(as_of)
                    and fact.supersedes != other.fact_id
                    and other.supersedes != fact.fact_id
                ):
                    conflicts.append(fact.fact_id)
                    break
        return tuple(dict.fromkeys(conflicts))
