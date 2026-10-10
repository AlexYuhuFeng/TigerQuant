"""Point-in-time evidence store.

The only read API is ``as_of``-bounded. There is deliberately no ``get_latest()``: a
caller that wants "the newest fact" must say *as of when*, which is what makes acceptance
gate 12 (no future revisions in replay) a property of the interface rather than of
reviewer discipline.

Three disciplines are enforced here rather than left to callers:

1. **Provenance is registered.** A fact may only enter the store for a source that exists
   in the :class:`~tigerquant.evidence.sources.SourceRegistry`; an unknown source is a
   hard error, not a new implicit source.
2. **Revisions are appended, never mutated.** :meth:`EvidenceStore.effective_fact` walks
   the ``supersedes`` chain forward only as far as ``as_of`` allows, so a replay at an
   earlier instant sees the revision that actually existed then.
3. **Freshness is per category.** :class:`~tigerquant.evidence.staleness.StalenessPolicy`
   decides what "stale" means for each kind of fact.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import ReasonCode
from tigerquant.core.errors import EvidenceError
from tigerquant.evidence.staleness import StalenessPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from tigerquant.evidence.facts import Fact
    from tigerquant.evidence.sources import SourceRegistry

__all__ = [
    "INJECTION_FLAG_PREFIX",
    "EvidenceStore",
    "Resolution",
    "ResolutionReport",
    "ResolutionStatus",
]

#: Quality-flag prefix that marks content as prompt-injection-suspected.
INJECTION_FLAG_PREFIX = "prompt_injection_suspected"


class ResolutionStatus:
    """Outcome of resolving one cited fact id."""

    RESOLVED = "RESOLVED"
    STALE = "STALE"
    MISSING = "MISSING"


class Resolution:
    """Per-fact resolution result."""

    __slots__ = ("effective", "fact", "reason_code", "status")

    def __init__(
        self,
        status: str,
        fact: Fact | None,
        reason_code: ReasonCode | None,
        *,
        effective: Fact | None = None,
    ) -> None:
        self.status = status
        self.fact = fact
        #: The revision in force at ``as_of`` (may differ from the cited revision).
        self.effective = effective if effective is not None else fact
        self.reason_code = reason_code

    def __repr__(self) -> str:
        name = None if self.fact is None else self.fact.fact_id
        return f"Resolution(status={self.status!r}, fact={name})"


class ResolutionReport:
    """Aggregate outcome of resolving a set of cited evidence ids."""

    __slots__ = (
        "as_of",
        "contradictions",
        "injection_flagged",
        "missing",
        "resolutions",
        "stale",
    )

    def __init__(
        self,
        *,
        as_of: dt.datetime,
        resolutions: dict[str, Resolution],
        missing: tuple[str, ...],
        stale: tuple[str, ...],
        contradictions: tuple[str, ...],
        injection_flagged: tuple[str, ...] = (),
    ) -> None:
        self.as_of = as_of
        self.resolutions = resolutions
        self.missing = missing
        self.stale = stale
        self.contradictions = contradictions
        #: Resolved facts whose content is injection-flagged. They are stored truthfully
        #: but may not, on their own, admit a trade (ADR-0005 §6).
        self.injection_flagged = injection_flagged

    @property
    def facts(self) -> tuple[Fact, ...]:
        return tuple(
            res.effective
            for res in self.resolutions.values()
            if res.status == ResolutionStatus.RESOLVED and res.effective is not None
        )

    @property
    def usable_facts(self) -> tuple[Fact, ...]:
        """Resolved facts that may support admission (injection-flagged ones excluded)."""
        return tuple(
            fact
            for fact in self.facts
            if not any(flag.startswith(INJECTION_FLAG_PREFIX) for flag in fact.quality_flags)
        )

    @property
    def resolved_ids(self) -> tuple[str, ...]:
        return tuple(
            fact_id
            for fact_id, res in self.resolutions.items()
            if res.status == ResolutionStatus.RESOLVED
        )

    @property
    def ok(self) -> bool:
        return not (self.missing or self.stale or self.contradictions)

    @property
    def admits_a_trade(self) -> bool:
        """Resolution succeeded *and* something usable remains to justify a trade."""
        return self.ok and bool(self.usable_facts)

    def reason_code(self) -> ReasonCode | None:
        """The single reason code that should force abstention, if any."""
        if self.missing:
            return ReasonCode.EVIDENCE_UNRESOLVED
        if self.stale:
            return ReasonCode.EVIDENCE_STALE
        if self.contradictions:
            return ReasonCode.EVIDENCE_CONTRADICTORY
        if not self.usable_facts:
            return ReasonCode.SNAPSHOT_INCOMPLETE
        return None

    def summary(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append(f"unresolved={list(self.missing)}")
        if self.stale:
            parts.append(f"stale={list(self.stale)}")
        if self.contradictions:
            parts.append(f"contradictions={list(self.contradictions)}")
        if self.injection_flagged:
            parts.append(f"injection_flagged={list(self.injection_flagged)}")
        return "; ".join(parts) if parts else "all evidence resolved"


class EvidenceStore:
    """Append-only, in-memory fact store with as-of reads.

    Facts are immutable and content-addressed; adding the same ``fact_id`` twice is a
    no-op, which makes ingestion idempotent (acceptance gate 7).
    """

    __slots__ = ("_by_key", "_facts", "_registry", "_successor")

    def __init__(self, registry: SourceRegistry, *, facts: Iterable[Fact] = ()) -> None:
        self._registry = registry
        self._facts: dict[str, Fact] = {}
        #: fact_id -> the fact that supersedes it.
        #:
        #: `vintages()` used to find successors by scanning EVERY fact, once per walk step, and
        #: it is called on the order of a hundred times per bar. A linear chain has at most one
        #: successor, so this is a single-entry map rather than a list, and `vintages()` stays
        #: linear in the NUMBER OF REVISIONS instead of in the size of the store.
        self._successor: dict[str, str] = {}
        #: (instrument, category, event_time) -> fact_ids that could contradict each other.
        #:
        #: Contradiction is scoped to that key, so only facts sharing it can ever conflict. The
        #: old code compared every cited fact against every stored fact -- one of the two
        #: quadratic loops. Grouping by the key the check already uses makes each comparison
        #: touch a handful of candidates instead of the whole store.
        self._by_key: dict[tuple[str, str, dt.datetime], list[str]] = {}
        for fact in facts:
            self.add(fact)

    @property
    def registry(self) -> SourceRegistry:
        return self._registry

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self) -> Iterator[Fact]:
        return iter(self._facts.values())

    def __contains__(self, fact_id: object) -> bool:
        return fact_id in self._facts

    def add(self, fact: Fact) -> bool:
        """Add ``fact``; return ``False`` when it was already present.

        Raises
        ------
        EvidenceError
            If the fact's source is not registered. Provenance is not free text.
        """
        self._registry.get(fact.source_id)
        if fact.fact_id in self._facts:
            return False
        self._facts[fact.fact_id] = fact

        # Maintain both indexes here, at the one place a fact enters the store, so they cannot
        # drift from `_facts`. Reads stay index-only; there is no rebuild path to forget.
        if fact.supersedes is not None:
            self._successor[fact.supersedes] = fact.fact_id
        if fact.instrument is not None and fact.value() is not None:
            key = (fact.instrument, fact.category, fact.event_time)
            self._by_key.setdefault(key, []).append(fact.fact_id)
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

    # -- revisions / vintages ----------------------------------------------------

    def vintages(self, fact_id: str) -> tuple[Fact, ...]:
        """The full ``supersedes`` chain containing ``fact_id``, oldest first.

        The chain is discoverable from the middle as well as from the root, because a
        caller may cite any revision.
        """
        start = self.get(fact_id)
        chain: list[Fact] = [start]
        seen = {start.fact_id}

        current = start
        while current.supersedes is not None and current.supersedes in self._facts:
            previous = self._facts[current.supersedes]
            if previous.fact_id in seen:
                break
            chain.insert(0, previous)
            seen.add(previous.fact_id)
            current = previous

        current = chain[-1]
        for _ in range(1_000):
            successor_id = self._successor.get(current.fact_id)
            if successor_id is None or successor_id in seen:
                break
            current = self._facts[successor_id]
            chain.append(current)
            seen.add(current.fact_id)
        return tuple(chain)

    def effective_fact(self, fact_id: str, as_of: dt.datetime) -> Fact:
        """The revision in force at ``as_of``.

        Revisions that were not yet available at ``as_of`` are ignored, so a replay at an
        earlier instant sees exactly what existed then (acceptance gate 12).
        """
        moment = ensure_utc(as_of)
        effective = self.get(fact_id)
        for candidate in self.vintages(fact_id):
            if candidate.is_known_at(moment):
                effective = candidate
        return effective

    # -- resolution --------------------------------------------------------------

    def resolve(
        self,
        fact_ids: Iterable[str],
        *,
        as_of: dt.datetime,
        staleness: StalenessPolicy | int,
    ) -> ResolutionReport:
        """Resolve cited evidence ids against the point-in-time view.

        Failure modes, all of which force abstention (invariant 4):

        * ``missing`` — the id does not exist, or existed only *after* ``as_of``
          (a model citing something it could not have seen is treated as fabrication);
        * ``stale`` — older than its category's horizon, or past ``valid_until``;
        * ``contradictions`` — two available facts of the same
          ``(instrument, category, event_time)`` disagree on ``value``.
        """
        policy = (
            staleness
            if isinstance(staleness, StalenessPolicy)
            else StalenessPolicy.uniform(staleness)
        )
        moment = ensure_utc(as_of)
        resolutions: dict[str, Resolution] = {}
        missing: list[str] = []
        stale: list[str] = []
        injection_flagged: list[str] = []
        seen: set[str] = set()

        for fact_id in fact_ids:
            if fact_id in seen:
                continue
            seen.add(fact_id)
            fact = self._facts.get(fact_id)
            if fact is None or not fact.is_known_at(moment):
                resolutions[fact_id] = Resolution(
                    ResolutionStatus.MISSING, None, ReasonCode.EVIDENCE_UNRESOLVED
                )
                missing.append(fact_id)
                continue

            effective = self.effective_fact(fact_id, moment)
            age = (moment - effective.available_at).total_seconds()
            if effective.is_stale(moment) or age > policy.for_fact(effective):
                resolutions[fact_id] = Resolution(
                    ResolutionStatus.STALE, fact, ReasonCode.EVIDENCE_STALE, effective=effective
                )
                stale.append(fact_id)
                continue

            resolutions[fact_id] = Resolution(
                ResolutionStatus.RESOLVED, fact, None, effective=effective
            )
            if any(flag.startswith(INJECTION_FLAG_PREFIX) for flag in effective.quality_flags):
                injection_flagged.append(fact_id)

        facts = [res.effective for res in resolutions.values() if res.effective is not None]
        contradictions = self._find_contradictions(facts, moment)
        return ResolutionReport(
            as_of=moment,
            resolutions=resolutions,
            missing=tuple(missing),
            stale=tuple(stale),
            contradictions=contradictions,
            injection_flagged=tuple(injection_flagged),
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
            # Only facts sharing the contradiction key can conflict, so the candidate set comes
            # from the index rather than from the whole store. Same comparisons, same result.
            candidates = self._by_key.get((fact.instrument, fact.category, fact.event_time), ())
            for other_id in candidates:
                other = self._facts[other_id]
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
