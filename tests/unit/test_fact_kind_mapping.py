"""A fact's epistemic kind is DERIVED from its source, not hardcoded.

`_to_fact` used to hardcode `kind=FactKind.ATTRIBUTED_CLAIM` with a comment explaining that a news
article is somebody's assertion and this system did not observe it. **That reasoning is correct for
news and wrong for everything else** -- a regulatory filing ingested through the same pipeline would
be labelled a mere claim.

`SourceKind.OFFICIAL_FILING` already existed in the enum. Nothing produced it and nothing honoured
it, so the distinction was anticipated and then not implemented: exactly the sort of half-built
thing that reads as complete.

The axis is PRIMARY versus SECONDARY, not true versus false. A filing and a wire story can report
the same number and are still different evidence.
"""

from __future__ import annotations

import pytest

from tigerquant.core.errors import ConfigurationError
from tigerquant.evidence.connectors import _SOURCE_KIND_TO_FACT_KIND, fact_kind_for
from tigerquant.evidence.facts import FactKind
from tigerquant.evidence.sources import SourceKind


class TestTheMapping:
    def test_a_filing_is_observed(self) -> None:
        """The document itself, not somebody's account of it. This is the case that was missing."""
        assert fact_kind_for(SourceKind.OFFICIAL_FILING) is FactKind.OBSERVED

    def test_a_wire_story_is_a_claim(self) -> None:
        """The original reasoning, preserved for the case it was actually about."""
        assert fact_kind_for(SourceKind.NEWS_WIRE) is FactKind.ATTRIBUTED_CLAIM

    def test_the_two_are_different(self) -> None:
        """THE point of the mapping. If these were equal, the distinction would be decorative."""
        assert fact_kind_for(SourceKind.OFFICIAL_FILING) is not fact_kind_for(SourceKind.NEWS_WIRE)

    def test_market_data_and_broker_state_are_observed(self) -> None:
        assert fact_kind_for(SourceKind.MARKET_DATA) is FactKind.OBSERVED
        assert fact_kind_for(SourceKind.BROKER) is FactKind.OBSERVED

    def test_derived_is_derived(self) -> None:
        assert fact_kind_for(SourceKind.DERIVED) is FactKind.DERIVED

    def test_every_source_kind_is_mapped(self) -> None:
        """A new SourceKind must not silently fall through. The refusal below catches it at
        runtime, but a missing entry is better found here."""
        unmapped = [kind.name for kind in SourceKind if kind not in _SOURCE_KIND_TO_FACT_KIND]
        assert unmapped == [], f"source kinds with no fact kind: {unmapped}"

    def test_nothing_is_mapped_to_estimate_or_belief(self) -> None:
        """Those are model outputs, not ingested documents. Mapping a SOURCE to them would let
        ingest claim a status only a model should carry."""
        values = set(_SOURCE_KIND_TO_FACT_KIND.values())
        assert FactKind.ESTIMATE not in values
        assert FactKind.PREDICTIVE_BELIEF not in values


class TestAnUnmappedKindRefuses:
    """The refusal guards a FUTURE SourceKind added without a mapping.

    Every current kind IS mapped -- asserted below -- so this branch is unreachable through the
    enum today, and that is the point: it is defence against the next addition, not against an
    existing case. Tested by removing an entry rather than by passing a string, because a string
    is a type error and would exercise a different path.
    """

    @pytest.fixture
    def unmapped(self, monkeypatch: pytest.MonkeyPatch) -> SourceKind:
        """A kind that exists but has no mapping, as a future addition would be."""
        mapping = dict(_SOURCE_KIND_TO_FACT_KIND)
        del mapping[SourceKind.OFFICIAL_FILING]
        monkeypatch.setattr(
            "tigerquant.evidence.connectors._SOURCE_KIND_TO_FACT_KIND", mapping, raising=True
        )
        return SourceKind.OFFICIAL_FILING

    def test_it_raises_rather_than_defaulting(self, unmapped: SourceKind) -> None:
        """Guessing is wrong in one of two directions -- promoting a claim to an observation, or
        demoting a primary document to a claim -- and nothing downstream could detect either,
        because the LABEL IS THE EVIDENCE."""
        with pytest.raises(ConfigurationError, match="has no fact kind"):
            fact_kind_for(unmapped)

    def test_the_refusal_says_what_to_do(self, unmapped: SourceKind) -> None:
        with pytest.raises(ConfigurationError) as caught:
            fact_kind_for(unmapped)
        assert "_SOURCE_KIND_TO_FACT_KIND" in str(caught.value)

    def test_the_refusal_lists_what_is_mapped(self, unmapped: SourceKind) -> None:
        """So the fix is a one-line edit rather than a search."""
        with pytest.raises(ConfigurationError) as caught:
            fact_kind_for(unmapped)
        assert "NEWS_WIRE" in str(caught.value)

    def test_the_refusal_names_the_missing_kind(self, unmapped: SourceKind) -> None:
        with pytest.raises(ConfigurationError) as caught:
            fact_kind_for(unmapped)
        assert "OFFICIAL_FILING" in str(caught.value)


class TestThePipelineUsesIt:
    def test_to_fact_no_longer_hardcodes_the_kind(self) -> None:
        """Asserted structurally, because the failure was a literal in the middle of a call: it
        read as deliberate and applied to everything."""
        import inspect

        from tigerquant.evidence import connectors

        source = inspect.getsource(connectors.IngestionPipeline._to_fact)
        assert "fact_kind_for(source.kind)" in source
        assert "kind=FactKind.ATTRIBUTED_CLAIM" not in source

    def test_the_mapping_lives_in_one_place(self) -> None:
        from tigerquant.evidence import connectors

        assert connectors._SOURCE_KIND_TO_FACT_KIND is _SOURCE_KIND_TO_FACT_KIND
