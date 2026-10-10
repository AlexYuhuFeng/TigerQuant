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

#: Categories known to the CR-01 slice. CR-05 adds news, filings and macro series.
KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "price_bar",
        "quote",
        "position_snapshot",
        "calendar_session",
        "news_claim",
        "filing_claim",
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
        return digest_of(self)


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
