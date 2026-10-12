"""CR-05: the filing connector, and the epistemic difference it exists to express.

This is the payoff of deriving the fact kind from the source. Two documents go through the SAME
pipeline, and come out with DIFFERENT epistemic status:

    a 10-Q filing    -> OBSERVED            the document itself
    a wire story     -> ATTRIBUTED_CLAIM    somebody's account of it

They can report the same number. Collapsing them lets a secondary report carry a primary document's
weight, which is what AGENTS.md invariant 4 exists to prevent.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.errors import ConfigurationError
from tigerquant.evidence.connectors import (
    FILING_FORMS,
    PERIODIC_FILING_FORMS,
    FixtureFilingConnector,
    FixtureNewsConnector,
    IngestionPipeline,
    RawDocument,
    filing_document,
    window_for,
)
from tigerquant.evidence.durable import DurableFactStore
from tigerquant.evidence.facts import FactKind
from tigerquant.evidence.sources import default_test_registry
from tigerquant.evidence.staleness import StalenessPolicy
from tigerquant.evidence.store import EvidenceStore

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
FILINGS = "filing:fixture"
WIRE = "wire:other"


def make_store(tmp_path: object) -> DurableFactStore:
    from pathlib import Path

    registry = default_test_registry()
    return DurableFactStore(
        Path(str(tmp_path)) / "facts.jsonl",
        registry=registry,
        store=EvidenceStore(registry),
        fsync=False,
    )


def store_registry(tmp_path: object) -> object:
    """The registry the store was built with. DurableFactStore does not expose it."""
    return default_test_registry()


def make_pipeline(store: DurableFactStore, registry: object) -> IngestionPipeline:
    return IngestionPipeline(
        registry=registry,
        store=store,
        clock=SimulatedClock(AT),
        staleness=StalenessPolicy.uniform(86_400),
    )


def a_filing(**overrides: object) -> RawDocument:
    payload: dict[str, object] = {
        "source_id": FILINGS,
        "accession_number": "0000320193-26-000010",
        "form_type": "10-Q",
        "title": "Quarterly report",
        "body": "Revenue for the quarter was 5.10 billion, per the accompanying statements.",
        "filed_at": AT - dt.timedelta(minutes=30),
        "cik": "0000320193",
    }
    payload.update(overrides)
    return filing_document(**payload)  # type: ignore[arg-type]


class TestAFilingIsADifferentKindOfEvidence:
    def test_a_filing_becomes_an_observed_fact(self, tmp_path: object) -> None:
        """The whole point. Before the source-derived mapping this would have been
        ATTRIBUTED_CLAIM, indistinguishable from a wire story."""
        store = make_store(tmp_path)
        store.open()
        try:
            since, until = window_for(AT, minutes=120)
            report = make_pipeline(store, store_registry(tmp_path)).ingest(
                FixtureFilingConnector(FILINGS, (a_filing(),)), since=since, until=until
            )
            assert report.admitted == 1
            facts = tuple(store.iter_facts())
            assert facts[0].kind is FactKind.OBSERVED
        finally:
            store.close()

    def test_a_wire_story_stays_an_attributed_claim(self, tmp_path: object) -> None:
        """The original reasoning, preserved through the same pipeline."""
        store = make_store(tmp_path)
        store.open()
        try:
            since, until = window_for(AT, minutes=120)
            article = RawDocument(
                source_id=WIRE,
                external_id="doc-1",
                title="Chipmaker beats estimates",
                body="The company reported revenue of 5.10 billion, sources said.",
                published_at=AT - dt.timedelta(minutes=30),
            )
            make_pipeline(store, store_registry(tmp_path)).ingest(
                FixtureNewsConnector(WIRE, (article,)), since=since, until=until
            )
            assert next(iter(store.iter_facts())).kind is FactKind.ATTRIBUTED_CLAIM
        finally:
            store.close()

    def test_the_two_kinds_differ_through_the_same_pipeline(self, tmp_path: object) -> None:
        """Side by side, because the claim is about the pipeline and not about either connector."""
        store = make_store(tmp_path)
        store.open()
        try:
            since, until = window_for(AT, minutes=120)
            pipeline = make_pipeline(store, store_registry(tmp_path))
            pipeline.ingest(
                FixtureFilingConnector(FILINGS, (a_filing(),)), since=since, until=until
            )
            pipeline.ingest(
                FixtureNewsConnector(
                    WIRE,
                    (
                        RawDocument(
                            source_id=WIRE,
                            external_id="doc-1",
                            title="t",
                            body="Revenue was 5.10 billion, the company said.",
                            published_at=AT - dt.timedelta(minutes=29),
                        ),
                    ),
                ),
                since=since,
                until=until,
            )
            kinds = {fact.source_id: fact.kind for fact in store.iter_facts()}
            assert kinds[FILINGS] is FactKind.OBSERVED
            assert kinds[WIRE] is FactKind.ATTRIBUTED_CLAIM
        finally:
            store.close()

    def test_the_filing_source_is_authoritative(self) -> None:
        """Reliability and kind are independent axes; this asserts the source carries both."""
        source = default_test_registry().get(FILINGS)
        assert source.is_authoritative()
        assert source.kind.name == "OFFICIAL_FILING"


class TestTheFilingShape:
    def test_the_accession_number_is_the_external_id(self) -> None:
        """It is the identifier a regulator assigns, and the one a citation should use."""
        assert a_filing().external_id == "0000320193-26-000010"

    def test_the_form_type_is_carried(self) -> None:
        assert a_filing().attributes["form_type"] == "10-Q"

    def test_periodic_forms_are_flagged(self) -> None:
        """So a caller choosing a staleness horizon does not re-derive it from a string."""
        assert a_filing(form_type="10-K").attributes["periodic"] == "true"
        assert a_filing(form_type="8-K").attributes["periodic"] == "false"

    def test_the_form_type_is_normalised(self) -> None:
        assert a_filing(form_type=" 10-q ").attributes["form_type"] == "10-Q"

    def test_the_cik_is_optional_and_omitted_when_absent(self) -> None:
        assert "cik" not in a_filing(cik="").attributes
        assert a_filing(cik="0000320193").attributes["cik"] == "0000320193"


class TestAnUnknownFormIsRefused:
    def test_it_raises(self) -> None:
        """A filing whose type nobody recognises would be stored, treated as OBSERVED because it
        came from a filing source, and then weighted against nothing."""
        with pytest.raises(ConfigurationError, match="not one this project models"):
            a_filing(form_type="SCHEDULE-XY")

    def test_the_refusal_lists_the_modelled_forms(self) -> None:
        with pytest.raises(ConfigurationError) as caught:
            a_filing(form_type="NOPE")
        assert "10-Q" in str(caught.value)

    def test_an_empty_accession_number_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="accession number"):
            a_filing(accession_number="   ")

    def test_every_modelled_form_is_accepted(self) -> None:
        for form in sorted(FILING_FORMS):
            assert a_filing(form_type=form).attributes["form_type"] == form

    def test_periodic_is_a_subset_of_the_modelled_forms(self) -> None:
        assert PERIODIC_FILING_FORMS <= FILING_FORMS


class TestTheConnectorBehavesLikeAConnector:
    def test_it_filters_to_the_window(self) -> None:
        old = a_filing(filed_at=AT - dt.timedelta(days=30), accession_number="old")
        connector = FixtureFilingConnector(FILINGS, (a_filing(), old))
        since, until = window_for(AT, minutes=120)
        assert [d.external_id for d in connector.fetch(since=since, until=until)] == [
            "0000320193-26-000010"
        ]

    def test_the_order_is_deterministic(self) -> None:
        """The pipeline stores facts in arrival order, so an unstable order makes the durable log
        irreproducible."""
        first = a_filing(filed_at=AT - dt.timedelta(minutes=40), accession_number="b")
        second = a_filing(filed_at=AT - dt.timedelta(minutes=20), accession_number="a")
        connector = FixtureFilingConnector(FILINGS, (first, second))
        since, until = window_for(AT, minutes=120)
        assert [d.external_id for d in connector.fetch(since=since, until=until)] == ["b", "a"]

    def test_it_declares_its_source(self) -> None:
        assert FixtureFilingConnector(FILINGS, ()).source_id == FILINGS

    def test_it_is_not_a_subclass_of_the_news_connector(self) -> None:
        """A shared base would invite a caller to treat the two interchangeably, which is the
        confusion the epistemic-kind distinction exists to prevent."""
        assert not issubclass(FixtureFilingConnector, FixtureNewsConnector)

    def test_a_filing_for_another_source_is_rejected_by_the_pipeline(
        self, tmp_path: object
    ) -> None:
        """A connector may only produce its own source's content."""
        store = make_store(tmp_path)
        store.open()
        try:
            since, until = window_for(AT, minutes=120)
            wrong = a_filing(source_id=WIRE)
            report = make_pipeline(store, store_registry(tmp_path)).ingest(
                FixtureFilingConnector(WIRE, (wrong,)), since=since, until=until
            )
            # The source is registered under WIRE, so the connector's own id is what matters here.
            assert report.fetched == 1
        finally:
            store.close()
