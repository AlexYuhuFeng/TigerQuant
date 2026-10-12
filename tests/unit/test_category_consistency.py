"""The three lists that describe a fact category must agree.

THIS PATTERN HAS NOW BEEN FOUND THREE TIMES, and each time it was a list on one side of an interface
disagreeing with a list on the other, with nothing comparing them:

1. The pipeline emitted categories derived from SOURCE KIND NAMES -- `OFFICIAL_FILING`,
   `NEWS_WIRE` -- which appeared nowhere in `DEFAULT_STALENESS.per_category`. Every ingested fact
   fell through to the 60-second default and the policy's thirty-day filing horizon was consulted by
   nothing. Silent, because a fact that reads as stale looks like a fact that IS stale.

2. `KNOWN_CATEGORIES` was missing `filing_event` (added to the policy a round earlier) and
   `derived_feature` (never added). It is also **exported and never consumed** -- a constant that
   looks like validation and validates nothing.

3. Four policy categories have no producer at all.

**The fix is not the corrections. It is the comparison**, which is what this file is.
"""

from __future__ import annotations

import inspect

import pytest

from tigerquant.evidence.connectors import _SOURCE_KIND_TO_CATEGORY
from tigerquant.evidence.facts import KNOWN_CATEGORIES
from tigerquant.evidence.staleness import DEFAULT_STALENESS

#: Categories a producer actually emits today, by inspection. Kept explicit rather than derived,
#: because "which producers exist" is a fact about the code that a test should state out loud.
PRODUCED_BY_EVIDENCE_INGESTION = frozenset(
    {
        "news_claim",
        "filing_claim",
        "filing_event",
        "position_snapshot",
        "derived_feature",
    }
)
PRODUCED_ELSEWHERE = frozenset({"price_bar"})


class TestTheListsAgree:
    def test_known_categories_matches_the_policy_exactly(self) -> None:
        """THE comparison. Two sets that are supposed to describe the same thing, asserted equal in
        both directions so neither a stale entry nor a missing one can hide."""
        assert set(DEFAULT_STALENESS.per_category) == KNOWN_CATEGORIES

    def test_neither_side_has_an_entry_the_other_lacks(self) -> None:
        """Stated separately so a failure says WHICH direction is wrong."""
        assert KNOWN_CATEGORIES - set(DEFAULT_STALENESS.per_category) == set()
        assert set(DEFAULT_STALENESS.per_category) - KNOWN_CATEGORIES == set()

    def test_every_produced_category_is_known_and_has_a_horizon(self) -> None:
        """A produced category that is not known gets the conservative default silently, which is
        how the original defect presented."""
        produced = PRODUCED_BY_EVIDENCE_INGESTION | PRODUCED_ELSEWHERE
        assert produced <= KNOWN_CATEGORIES
        assert produced <= set(DEFAULT_STALENESS.per_category)

    def test_every_mapped_source_category_is_known(self) -> None:
        assert set(_SOURCE_KIND_TO_CATEGORY.values()) <= KNOWN_CATEGORIES


class TestTheUnproducedCategoriesAreNamed:
    """Policy entries nothing produces are not automatically wrong -- a deployment may reserve a
    horizon for a feed it has not connected. But they must be NAMED, so the inventory cannot grow
    silently and a reader is not misled into thinking they are live."""

    #: Categories with a horizon and, today, no producer.
    RESERVED = frozenset({"quote", "calendar_session"})

    def test_the_reserved_set_is_exactly_the_unproduced_ones(self) -> None:
        produced = PRODUCED_BY_EVIDENCE_INGESTION | PRODUCED_ELSEWHERE
        unproduced = set(DEFAULT_STALENESS.per_category) - produced
        assert unproduced == self.RESERVED, (
            f"the set of categories with no producer changed: now {sorted(unproduced)}. If a feed "
            "was connected, remove it from RESERVED; if a horizon was added with nothing to use "
            "it, that is configuration that looks live and is not."
        )

    def test_a_reserved_category_still_has_a_horizon(self) -> None:
        """Reserved is not the same as broken: connecting a quote feed should not require also
        choosing a number."""
        for category in self.RESERVED:
            assert DEFAULT_STALENESS.max_age_seconds(category) > 0


class TestKnownCategoriesIsNotEnforced:
    """Recorded rather than smoothed over."""

    def test_nothing_validates_a_fact_category_against_it(self) -> None:
        """A constant that looks like validation and validates nothing is the shape of defect
        AGENTS.md invariant 11 is about. This asserts the CURRENT state so that wiring it later is a
        deliberate change rather than a silent one."""
        from tigerquant.evidence.facts import Fact

        source = inspect.getsource(Fact)
        assert "KNOWN_CATEGORIES" not in source, (
            "KNOWN_CATEGORIES is now enforced; update this test and the limitation it records"
        )

    def test_a_fact_with_an_unknown_category_is_accepted_today(self) -> None:
        """The consequence: a typo'd category is stored and silently takes the conservative
        default. Stated as a fact rather than left implied."""
        import datetime as dt

        from tigerquant.evidence.facts import Fact, FactKind

        at = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
        fact = Fact(
            fact_id="fact:test",
            kind=FactKind.OBSERVED,
            category="nwes_claim",  # a typo, deliberately
            instrument=None,
            event_time=at,
            published_at=at,
            available_at=at,
            ingested_at=at,
            valid_until=at + dt.timedelta(hours=1),
            source_id="wire:other",
            content_hash="sha256:" + "0" * 64,
        )
        assert fact.category == "nwes_claim"
        assert DEFAULT_STALENESS.max_age_seconds(fact.category) == DEFAULT_STALENESS.default_seconds

    def test_the_constant_is_exported(self) -> None:
        """So the correction reaches callers rather than only the module."""
        from tigerquant.evidence import facts as package

        assert package.KNOWN_CATEGORIES == KNOWN_CATEGORIES


class TestThePolicyItselfIsCoherent:
    def test_every_horizon_is_positive(self) -> None:
        assert all(seconds > 0 for seconds in DEFAULT_STALENESS.per_category.values())

    def test_the_default_is_no_looser_than_the_loosest_category(self) -> None:
        assert DEFAULT_STALENESS.default_seconds <= max(DEFAULT_STALENESS.per_category.values())

    @pytest.mark.parametrize("category", sorted(KNOWN_CATEGORIES))
    def test_each_category_resolves(self, category: str) -> None:
        assert DEFAULT_STALENESS.max_age_seconds(category) > 0
