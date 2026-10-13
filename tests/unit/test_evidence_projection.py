"""Evidence facts now reach the projection -- closing the gap this file replaces.

`evidence_facts` was declared in the schema, both repositories implemented writing and the
point-in-time read, and **NOTHING CALLED EITHER**. A table that is never populated looks exactly like a
table that works, and the query that makes the projection worth having had nothing to read.

Replaces `test_evidence_projection_gap.py`, which existed only to mark that gap. Its failure messages
said so: *"If evidence-fact projection was implemented, THIS FILE SHOULD BE DELETED rather than
updated."*

USES THE IN-MEMORY REPOSITORY, deliberately: it implements `add_fact` too, so the contract is provable
without PostgreSQL and without the OS temp directory -- neither of which the local sandbox provides,
so a test needing them would be verified only by CI.
"""

from __future__ import annotations

import datetime as dt

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Mode
from tigerquant.core.ids import IdFactory
from tigerquant.db.projector import project
from tigerquant.db.repository import InMemoryRepository
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.journal import MemoryJournal
from tigerquant.evidence.connectors import FixtureNewsConnector, IngestionPipeline, RawDocument
from tigerquant.evidence.durable import DurableFactStore
from tigerquant.evidence.sources import default_test_registry
from tigerquant.evidence.staleness import DEFAULT_STALENESS
from tigerquant.evidence.store import EvidenceStore

AT = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)
WIRE = "wire:other"


def wired(tmp_path: object) -> tuple[IngestionPipeline, MemoryJournal, object, object]:
    """A pipeline wired to a journal, built from VERIFIED signatures.

    `journal.records` is a property returning `JournalRecord` objects that WRAP the event -- unwrapped
    with `record.event`, which is where the first attempt stopped.
    """
    from pathlib import Path

    registry = default_test_registry()
    store = DurableFactStore(
        Path(str(tmp_path)) / "facts.jsonl",
        registry=registry,
        store=EvidenceStore(registry),
        fsync=False,
    )
    store.open()
    clock = SimulatedClock(AT)
    journal = MemoryJournal(Mode.BACKTEST)
    emitter = EventEmitter(
        mode=Mode.BACKTEST,
        sink=journal,
        ids=IdFactory("projectiontest", clock),
        clock=clock,
        correlation_id="projectiontest",
    )
    pipeline = IngestionPipeline(
        registry=registry,
        store=store,
        clock=clock,
        staleness=DEFAULT_STALENESS,
        emitter=emitter,
    )
    return pipeline, journal, store, clock


def events_of(journal: MemoryJournal) -> list[object]:
    """The events inside the journal records. **The records WRAP them** -- `record.event`."""
    return [record.event for record in journal.records]


def article(external_id: str = "doc-1") -> RawDocument:
    return RawDocument(
        source_id=WIRE,
        external_id=external_id,
        title="Chipmaker beats estimates",
        body="Revenue was 5.10 billion, the company said.",
        published_at=AT - dt.timedelta(minutes=30),
    )


def ingest(pipeline: IngestionPipeline) -> object:
    return pipeline.ingest(
        FixtureNewsConnector(WIRE, (article(),)),
        since=AT - dt.timedelta(hours=2),
        until=AT,
    )


def project_all(journal: MemoryJournal) -> InMemoryRepository:
    repository = InMemoryRepository()
    project(events_of(journal), repository)
    return repository


class TestTheFactReachesTheJournal:
    def test_an_admitted_fact_is_emitted(self, tmp_path: object) -> None:
        """Without this the projector branch would be built-but-unused -- the very gap being closed."""
        pipeline, journal, store, _ = wired(tmp_path)
        try:
            assert ingest(pipeline).admitted == 1
            kinds = [type(event).__name__ for event in events_of(journal)]
            assert "EvidenceFactRecorded" in kinds, f"no evidence event emitted: {kinds}"
        finally:
            store.close()

    def test_a_pipeline_without_an_emitter_is_unchanged(self, tmp_path: object) -> None:
        """THE EMITTER IS OPTIONAL. Ingestion and projection are separate concerns, and making it
        mandatory would have forced every offline ingest to grow a journal."""
        from pathlib import Path

        registry = default_test_registry()
        store = DurableFactStore(
            Path(str(tmp_path)) / "f2.jsonl",
            registry=registry,
            store=EvidenceStore(registry),
            fsync=False,
        )
        store.open()
        try:
            pipeline = IngestionPipeline(
                registry=registry,
                store=store,
                clock=SimulatedClock(AT),
                staleness=DEFAULT_STALENESS,
            )
            assert ingest(pipeline).admitted == 1
        finally:
            store.close()

    def test_a_duplicate_is_not_re_emitted(self, tmp_path: object) -> None:
        """A duplicate is already projected; re-emitting would put a second journal entry against a
        fact that did not change."""
        pipeline, journal, store, _ = wired(tmp_path)
        try:
            ingest(pipeline)
            before = len(events_of(journal))
            ingest(pipeline)
            assert len(events_of(journal)) == before
        finally:
            store.close()


class TestTheFactReachesTheRepository:
    def test_the_projection_writes_the_fact(self, tmp_path: object) -> None:
        """THE thing that was missing: the row existed in the schema and nothing wrote it."""
        pipeline, journal, store, _ = wired(tmp_path)
        try:
            ingest(pipeline)
            assert len(project_all(journal).facts_as_of(AT)) == 1
        finally:
            store.close()

    def test_the_round_trip_preserves_the_fact(self, tmp_path: object) -> None:
        """The event carries the Fact's OWN JSON and the projector rebuilds with `model_validate`.
        Mapping fifteen columns into a payload would create a second definition of `Fact`."""
        pipeline, journal, store, _ = wired(tmp_path)
        try:
            ingest(pipeline)
            projected = project_all(journal).facts_as_of(AT)[0]
            original = next(iter(store.iter_facts()))
            assert projected.fact_id == original.fact_id
            assert projected.kind is original.kind
            assert projected.category == original.category
            assert projected.content_hash == original.content_hash
            assert projected.source_id == original.source_id
        finally:
            store.close()

    def test_the_point_in_time_read_works(self, tmp_path: object) -> None:
        """What did we know at that instant -- the query that makes the projection worth having."""
        pipeline, journal, store, _ = wired(tmp_path)
        try:
            ingest(pipeline)
            repository = project_all(journal)
            assert repository.facts_as_of(AT + dt.timedelta(days=1))
            assert repository.facts_as_of(AT - dt.timedelta(days=30)) == ()
        finally:
            store.close()

    def test_projection_is_idempotent(self, tmp_path: object) -> None:
        """Re-projecting is expected after a crash, and must not duplicate the row."""
        pipeline, journal, store, _ = wired(tmp_path)
        try:
            ingest(pipeline)
            repository = project_all(journal)
            project(events_of(journal), repository)
            assert len(repository.facts_as_of(AT)) == 1
        finally:
            store.close()


class TestTheEventIsRegistered:
    def test_the_type_is_known_to_the_registry(self) -> None:
        """An unregistered event type is a HARD ERROR by design: silently skipping an unrecognised
        record would let a partially-upgraded deployment replay a journal while ignoring half of it."""
        from tigerquant.events.registry import EVENT_TYPES

        assert "EvidenceFactRecorded" in EVENT_TYPES

    def test_it_declares_the_expected_type_name(self) -> None:
        from tigerquant.events.registry import EVENT_TYPES

        assert EVENT_TYPES["EvidenceFactRecorded"].model_fields["event_type"].default == (
            "EvidenceFactRecorded"
        )
