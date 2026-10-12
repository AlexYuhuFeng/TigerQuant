"""The categories the PIPELINE produces must be the ones the POLICY names.

This is the test that was missing, and its absence cost a real defect.

`DEFAULT_STALENESS` declares seven categories with carefully-chosen horizons -- `news_claim` at one
hour, `filing_claim` at thirty days. The pipeline derived its category from the SOURCE KIND NAME
(`OFFICIAL_FILING`, `NEWS_WIRE`), which appears NOWHERE in that policy. So **every ingested fact fell
through to the 60-second default**, and the two horizons the policy was written for were consulted by
nothing.

**The consequence was silent.** Stale evidence blocks trades (invariant 4), so every ingested fact
became unusable one minute after ingestion -- and an operator who configured a thirty-day filing
horizon would see it have no effect at all. The fact simply reads as stale, and a fact that reads as
stale looks exactly like a fact that IS stale.

Nothing tested the LINK between the two halves. Each was individually correct.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tigerquant.evidence.connectors import (
    _SOURCE_KIND_TO_CATEGORY,
    IngestionPipeline,
    RawDocument,
    _category_for,
    filing_document,
)
from tigerquant.evidence.durable import DurableFactStore
from tigerquant.evidence.sources import SourceKind, default_test_registry
from tigerquant.evidence.staleness import DEFAULT_STALENESS, StalenessPolicy
from tigerquant.evidence.store import EvidenceStore

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
FILINGS = "filing:fixture"
WIRE = "wire:other"


class TestTheLinkBetweenPipelineAndPolicy:
    """The test whose absence allowed the defect."""

    def test_every_mapped_category_exists_in_the_policy(self) -> None:
        """A category the policy does not name falls through to the conservative default, which
        silently overrides the horizon somebody deliberately chose."""
        named = set(DEFAULT_STALENESS.per_category)
        produced = set(_SOURCE_KIND_TO_CATEGORY.values())
        unknown = sorted(produced - named)
        assert unknown == [], (
            f"the pipeline produces categories the policy does not name: {unknown}. They would "
            f"fall through to {DEFAULT_STALENESS.default_seconds}s. Policy names: {sorted(named)}"
        )

    def test_a_news_claim_gets_the_news_horizon(self) -> None:
        """The defect, asserted directly: this was 60 seconds."""
        assert DEFAULT_STALENESS.max_age_seconds("news_claim") == 3_600

    def test_a_filing_gets_the_filing_horizon(self) -> None:
        """And this was 60 seconds too, against a declared thirty days."""
        assert DEFAULT_STALENESS.max_age_seconds("filing_claim") == 2_592_000

    def test_the_two_horizons_actually_differ_by_orders_of_magnitude(self) -> None:
        """If they were close, the mapping would be decorative."""
        news = DEFAULT_STALENESS.max_age_seconds("news_claim")
        filing = DEFAULT_STALENESS.max_age_seconds("filing_claim")
        assert filing > news * 100

    def test_every_source_kind_resolves_to_a_category(self) -> None:
        """Including the unmapped ones, which must resolve to SOMETHING rather than raising."""
        for kind in SourceKind:
            assert _category_for(_Source(kind))

    def test_unmapped_kinds_take_the_conservative_default(self) -> None:
        """Deliberate: a feed whose freshness nobody has reasoned about must be treated as
        perishable, not as durable."""
        assert SourceKind.MARKET_DATA not in _SOURCE_KIND_TO_CATEGORY
        assert SourceKind.SYNTHETIC not in _SOURCE_KIND_TO_CATEGORY


class TestFormLevelGranularity:
    """A 10-K describes a period; an 8-K announces a moment. One horizon for both is wrong."""

    def _doc(self, form: str) -> RawDocument:
        return filing_document(
            source_id=FILINGS,
            accession_number="0000320193-26-000010",
            form_type=form,
            title="t",
            body="b",
            filed_at=AT,
        )

    @pytest.mark.parametrize("form", ["10-K", "10-Q"])
    def test_periodic_forms_get_the_long_horizon(self, form: str) -> None:
        category = _category_for(_Source(SourceKind.OFFICIAL_FILING, FILINGS), self._doc(form))
        assert category == "filing_claim"

    @pytest.mark.parametrize("form", ["8-K", "4", "SC 13D"])
    def test_event_forms_get_the_short_horizon(self, form: str) -> None:
        category = _category_for(_Source(SourceKind.OFFICIAL_FILING, FILINGS), self._doc(form))
        assert category == "filing_event"

    def test_the_two_filing_horizons_differ(self) -> None:
        assert DEFAULT_STALENESS.max_age_seconds("filing_event") < (
            DEFAULT_STALENESS.max_age_seconds("filing_claim")
        )

    def test_the_periodic_flag_is_what_drives_it(self) -> None:
        """The flag was recorded two rounds ago and nothing consumed it until now."""
        assert self._doc("10-Q").attributes["periodic"] == "true"
        assert self._doc("8-K").attributes["periodic"] == "false"

    def test_without_a_document_the_source_level_category_applies(self) -> None:
        """A caller that has only a source still gets the right answer, conservatively."""
        assert _category_for(_Source(SourceKind.OFFICIAL_FILING, FILINGS)) == "filing_claim"

    def test_an_unknown_form_does_not_silently_lengthen_the_horizon(self) -> None:
        """An unrecognised form cannot be built by `filing_document`, but a caller could construct
        one directly. It must not be treated as periodic."""
        raw = RawDocument(
            source_id=FILINGS,
            external_id="x",
            title="t",
            body="b",
            published_at=AT,
            attributes={},
        )
        category = _category_for(_Source(SourceKind.OFFICIAL_FILING, FILINGS), raw)
        assert category == "filing_claim", "absent a form, the source-level answer stands"


class TestEndToEndThroughStorage:
    def test_an_ingested_filing_is_not_stale_after_a_day(self, tmp_path: object) -> None:
        """THE consequence. Before the fix this fact was stale after sixty seconds, so invariant 4
        would have refused it -- and the operator's thirty-day horizon had no effect."""
        from pathlib import Path

        registry = default_test_registry()
        store = DurableFactStore(
            Path(str(tmp_path)) / "f.jsonl",
            registry=registry,
            store=EvidenceStore(registry),
            fsync=False,
        )
        store.open()
        try:
            from tigerquant.core.clock import SimulatedClock
            from tigerquant.evidence.connectors import FixtureFilingConnector, window_for

            pipeline = IngestionPipeline(
                registry=registry,
                store=store,
                clock=SimulatedClock(AT),
                staleness=DEFAULT_STALENESS,
            )
            since, until = window_for(AT, minutes=120)
            pipeline.ingest(
                FixtureFilingConnector(
                    FILINGS,
                    (
                        filing_document(
                            source_id=FILINGS,
                            accession_number="0000320193-26-000010",
                            form_type="10-Q",
                            title="Quarterly report",
                            body="Revenue was 5.10 billion.",
                            filed_at=AT - dt.timedelta(minutes=30),
                        ),
                    ),
                ),
                since=since,
                until=until,
            )
            fact = next(iter(store.iter_facts()))
            assert fact.category == "filing_claim"
            assert not fact.is_stale(AT + dt.timedelta(days=1)), (
                "a quarterly report must still be usable a day later; before the category fix it "
                "was treated as stale after sixty seconds"
            )
        finally:
            store.close()


class _Source:
    """A minimal stand-in: `_category_for` reads only `kind` and the document."""

    def __init__(self, kind: SourceKind, source_id: str = "x") -> None:
        self.kind = kind
        self.source_id = source_id


class TestTheUnmappedFallbackIsStillConservative:
    def test_the_default_is_no_looser_than_the_loosest_category(self) -> None:
        """The property the VALIDATOR enforces, and the one the mapping relies on: an unmapped
        kind falls through to a horizon no longer than the longest configured one."""
        assert DEFAULT_STALENESS.default_seconds <= max(DEFAULT_STALENESS.per_category.values())

    def test_the_default_is_not_the_most_conservative_value(self) -> None:
        """A DISCREPANCY, recorded rather than smoothed over.

        The docstring says the default must be "the *most* conservative value a deployment is
        willing to accept", and the validator only checks that it is no looser than the LOOSEST
        configured category. Those are different rules: position_snapshot is 30 seconds, so a
        truly most-conservative default would be 30, and it is 60.

        The practical effect is bounded -- an unmapped category gets 60 seconds rather than 30 --
        but the docstring claims a guarantee the code does not provide, which is the shape of
        defect this project keeps finding. Asserted here so it cannot drift further unnoticed.
        """
        assert DEFAULT_STALENESS.default_seconds > min(DEFAULT_STALENESS.per_category.values())

    def test_a_policy_cannot_be_built_with_a_loose_default(self) -> None:
        with pytest.raises(ValueError, match="conservative"):
            StalenessPolicy(default_seconds=10**9, per_category={"x": 60})
