"""Derived facts and the claim-to-evidence graph.

A *derived* fact is computed by us from other facts. The value of the claim-to-evidence
graph is that a derived number can always be traced back to the observations it came from,
and that the traceability is enforced rather than documented:

* a derived fact must cite at least one input fact id;
* every input must exist, and must have been available at the derived fact's
  ``available_at`` — a derivation cannot depend on something that did not exist yet;
* the derived fact's ``available_at`` is the **maximum** of its inputs' ``available_at``,
  which is the formal no-lookahead rule of ADR-0005 §2;
* the inputs' categories drive the derived fact's own freshness horizon.

The last point is what stops a long-lived filing from keeping a minutes-old price fresh:
``effective_max_age`` is the *minimum* horizon across the inputs, so the most perishable
input governs.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from tigerquant.core.canonical import digest_of
from tigerquant.core.enums import ReasonCode
from tigerquant.core.errors import EvidenceError
from tigerquant.core.money import dec_str
from tigerquant.evidence.facts import Fact, FactKind, fact_id_for
from tigerquant.evidence.staleness import StalenessPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tigerquant.evidence.store import EvidenceStore

__all__ = ["DERIVED_SOURCE_ID", "DerivedFeature", "derive_fact", "effective_max_age"]

#: Source id every locally-computed fact is attributed to.
DERIVED_SOURCE_ID = "derived:local"

#: Bumped whenever a derivation changes, so a stored value can be reproduced exactly.
CALCULATION_VERSION = "derived-1.0.0"


class DerivedFeature(BaseModel):
    """A deterministic computation over input facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")]
    #: Human-readable formula, stored with the value so it can be reviewed later.
    formula: Annotated[str, Field(min_length=1)]
    value: Annotated[str, Field(min_length=1)]
    unit: Annotated[str, Field(min_length=1)] = "ratio"
    calculation_version: str = CALCULATION_VERSION

    def digest(self) -> str:
        return digest_of(self)


def effective_max_age(inputs: Sequence[Fact], policy: StalenessPolicy) -> int:
    """The tightest freshness horizon among ``inputs``.

    A derived value is only as trustworthy as its most perishable input, so the derived
    fact inherits the *minimum* horizon rather than, say, the maximum or the default.
    """
    if not inputs:
        msg = "a derived fact requires at least one input"
        raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED)
    return min(policy.for_fact(fact) for fact in inputs)


def derive_fact(
    *,
    feature: DerivedFeature,
    inputs: Sequence[Fact],
    instrument: str | None,
    store: EvidenceStore,
    source_id: str = DERIVED_SOURCE_ID,
    policy: StalenessPolicy | None = None,
    supersedes: str | None = None,
) -> Fact:
    """Build a derived fact, enforcing lineage and the no-lookahead rule.

    Parameters
    ----------
    feature:
        The computation and its result.
    inputs:
        Facts the result depends on. Must be non-empty and must already resolve in
        ``store`` (a derivation cannot invent its own inputs).
    instrument:
        Instrument the derived value is about, or ``None`` for a market-wide value.
    store:
        The store the inputs must already exist in. Passed explicitly so an unregistered
        or fabricated input cannot be smuggled into the graph.
    policy:
        Staleness policy used to derive the new fact's freshness horizon. Defaults to the
        import-time :data:`~tigerquant.evidence.staleness.DEFAULT_STALENESS`.
    """
    if not inputs:
        msg = "a derived fact requires at least one input fact"
        raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED)

    known: dict[str, Fact] = {}
    for fact in inputs:
        stored = store.get(fact.fact_id)  # raises when unknown
        if stored.digest() != fact.digest():
            msg = (
                f"input fact {fact.fact_id} does not match the stored record; a derivation "
                "must use the store's immutable content"
            )
            raise EvidenceError(msg, ReasonCode.EVIDENCE_CONTRADICTORY)
        known[fact.fact_id] = stored

    available_at = max(fact.available_at for fact in known.values())
    event_time = max(fact.event_time for fact in known.values())
    ingested_at = max(fact.ingested_at for fact in known.values())
    published_at = max(fact.published_at for fact in known.values())

    if policy is not None:
        horizon = effective_max_age(tuple(known.values()), policy)
    else:
        from tigerquant.evidence.staleness import DEFAULT_STALENESS

        horizon = effective_max_age(tuple(known.values()), DEFAULT_STALENESS)

    content_hash = digest_of(
        {
            "feature": feature,
            "inputs": [
                {"fact_id": fact.fact_id, "digest": fact.digest()} for fact in known.values()
            ],
            "instrument": instrument,
        }
    )
    return Fact(
        fact_id=fact_id_for(
            category="derived_feature",
            instrument=instrument,
            source_id=source_id,
            event_time=event_time,
            content_hash=content_hash,
        ),
        kind=FactKind.DERIVED,
        category="derived_feature",
        instrument=instrument,
        event_time=event_time,
        published_at=published_at,
        # A derived value cannot be known before its last input was.
        available_at=available_at,
        ingested_at=max(ingested_at, available_at),
        valid_until=available_at + dt.timedelta(seconds=horizon),
        source_id=source_id,
        content_hash=content_hash,
        supersedes=supersedes,
        attributes={
            "value": feature.value,
            "feature": feature.name,
            "formula": feature.formula,
            "unit": feature.unit,
            "calculation_version": feature.calculation_version,
            # Lineage is recorded on the fact itself so the graph survives without the
            # original computation code.
            "inputs": ",".join(sorted(known)),
            "input_count": str(len(known)),
        },
    )


def momentum_feature(*, closes: Sequence[str], lookback: int) -> DerivedFeature:
    """Percentage price change over ``lookback`` bars, in basis points.

    Kept here rather than in the strategy so the value has a stable name and formula in
    the evidence graph; the strategy only decides what to do about it.
    """
    from decimal import Decimal

    if lookback < 1:
        msg = "lookback must be at least 1"
        raise ValueError(msg)
    if len(closes) < lookback + 1:
        msg = f"need {lookback + 1} closes to compute a {lookback}-bar momentum, got {len(closes)}"
        raise ValueError(msg)
    oldest = Decimal(closes[0])
    newest = Decimal(closes[lookback])
    if oldest <= 0:
        msg = f"oldest close must be positive, got {oldest}"
        raise ValueError(msg)
    bps = (newest / oldest - Decimal(1)) * Decimal(10_000)
    return DerivedFeature(
        name="momentum_bps",
        formula=f"(close[t] / close[t-{lookback}] - 1) * 10000",
        value=dec_str(bps),
        unit="bps",
    )


def attribute_map(fact: Fact) -> Mapping[str, str]:
    """Read-only view of a fact's attributes, for reporting and tests."""
    return dict(fact.attributes)
