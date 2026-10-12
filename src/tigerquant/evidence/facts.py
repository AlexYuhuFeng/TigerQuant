"""Typed facts with full provenance.

A fact is an immutable, content-addressed record of one observation, derivation,
attributed claim, estimate or predictive belief. ``verified`` (implied by a fact being
in the store at all) attests that the record is well-formed, attributable and internally
consistent — it never asserts that a market theory is true (ADR-0005 §1).

Every fact carries the five timestamps of ADR-0005 §2. ``available_at`` is the one that
governs admissibility.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tigerquant.core.canonical import digest_of
from tigerquant.events.base import AwareUTC, NonEmptyStr

__all__ = ["Fact", "FactKind", "fact_id_for"]

#: Every category this project recognises.
#:
#: MUST match `DEFAULT_STALENESS.per_category` in both directions, and a test asserts it. It did not:
#: `filing_event` was added to the policy and not here, and `derived_feature` never was. Two lists
#: that are supposed to describe the same thing, with nothing comparing them.
#:
#: NOTE: this constant is currently EXPORTED AND NOT ENFORCED. Nothing validates a fact's category
#: against it, so a typo silently gets the conservative staleness default -- the same shape of defect
#: as the one it was just corrected for. Wiring it into `Fact` would reject unknown categories, which
#: is the right behaviour and a change with real blast radius; recorded as a limitation rather than
#: made late in a round.
KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "price_bar",
        "quote",
        "position_snapshot",
        "calendar_session",
        "news_claim",
        "filing_claim",
        "filing_event",
        "derived_feature",
    }
)


class FactKind(StrEnum):
    """What kind of statement a fact makes. Determines how it may be used."""

    #: Directly observed from a source (a bar, a quote, a broker position).
    OBSERVED = "OBSERVED"
    #: Computed deterministically from other facts; lineage is recorded.
    DERIVED = "DERIVED"
    #: A claim attributed to a named source, e.g. a news item. Not truth.
    ATTRIBUTED_CLAIM = "ATTRIBUTED_CLAIM"
    #: A modelled estimate with an explicit method.
    ESTIMATE = "ESTIMATE"
    #: A forward-looking belief. May never be cited as a fact about the present.
    PREDICTIVE_BELIEF = "PREDICTIVE_BELIEF"


#: fact_id -> digest. Bounded; see `Fact.digest`.
#:
#: Keyed by fact_id rather than by the object because a Fact holds a dict attribute and is
#: therefore unhashable. The key is sound because this project derives `fact_id` from content.
_DIGEST_CACHE: dict[str, str] = {}
_DIGEST_CACHE_LIMIT = 200_000


class Fact(BaseModel):
    """One immutable, attributable record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: NonEmptyStr
    kind: FactKind
    category: NonEmptyStr
    instrument: str | None = None

    event_time: AwareUTC
    published_at: AwareUTC
    available_at: AwareUTC
    ingested_at: AwareUTC
    valid_until: AwareUTC

    source_id: NonEmptyStr
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    #: Redistribution scope; ``internal`` content must not be exported (ADR-0005 §7).
    license_scope: NonEmptyStr = "internal"
    quality_flags: tuple[str, ...] = ()
    #: Fact this one revises, if any. Revisions are appended, never mutated.
    supersedes: str | None = None
    #: Canonical string values used by deterministic recomputation (ADR-0006).
    attributes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_time_order(self) -> Fact:
        if self.available_at < self.event_time:
            msg = "available_at precedes event_time; a value cannot be known before it exists"
            raise ValueError(msg)
        if self.ingested_at < self.available_at:
            msg = "ingested_at precedes available_at; ingestion cannot be earlier than availability"
            raise ValueError(msg)
        if self.valid_until < self.event_time:
            msg = "valid_until precedes event_time; the fact would be born stale"
            raise ValueError(msg)
        return self

    def is_stale(self, as_of: dt.datetime) -> bool:
        """True when ``as_of`` is past this fact's staleness horizon."""
        return as_of > self.valid_until

    def is_known_at(self, as_of: dt.datetime) -> bool:
        """True when this fact was legitimately available at ``as_of``."""
        return self.available_at <= as_of

    def value(self) -> str | None:
        """The primary numeric/string value, if this fact carries one."""
        return self.attributes.get("value")

    def digest(self) -> str:
        """A content digest, memoised because it cannot change.

        A Fact is frozen, so this is a pure function of state: computing it once and reusing the
        result is observationally identical to recomputing it, and the returned bytes are the
        same. That matters here specifically -- the snapshot digest embeds this value and CR-01's
        baseline journal digest pins the snapshot digests, so an optimisation that changed the
        output would have moved a published baseline. This one cannot.

        It was worth doing because a snapshot emits one of these per CITED fact and the strategy
        cites every bar it has seen, which made the total number of digest calls grow with the
        square of the run length: 5,360 calls at 100 bars, 81,798 at 400.
        """
        cached = _DIGEST_CACHE.get(self.fact_id)
        if cached is not None:
            return cached
        value = digest_of(self)
        if len(_DIGEST_CACHE) >= _DIGEST_CACHE_LIMIT:
            # Bounded so a long run cannot grow the cache without limit. Evicting the oldest
            # half rather than clearing keeps it useful under steady state.
            for key in list(_DIGEST_CACHE)[: _DIGEST_CACHE_LIMIT // 2]:
                del _DIGEST_CACHE[key]
        _DIGEST_CACHE[self.fact_id] = value
        return value


def fact_id_for(
    *,
    category: str,
    instrument: str | None,
    source_id: str,
    event_time: dt.datetime,
    content_hash: str,
) -> str:
    """Deterministic fact identifier (ADR-0011 §2)."""
    material = "|".join(
        [
            category,
            instrument or "",
            source_id,
            event_time.astimezone(dt.UTC).isoformat(timespec="microseconds"),
            content_hash,
        ]
    )
    return f"fact:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


#: Attributes that are always present on an observed price fact.
_PRICE_ATTRS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def price_attributes(
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: int,
) -> dict[str, str]:
    """Canonical attribute dict for a price bar fact."""
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": str(volume),
        "value": close,
    }


def price_attribute_names() -> tuple[str, ...]:
    return _PRICE_ATTRS
