"""The two EvidenceStore indexes, and why they exist.

`vintages()` and `_find_contradictions()` used to scan every stored fact on every call.
Profiling found them together accounting for most of a quadratic replay
(docs/evidence/cr09_scaling_investigation.txt).

These tests pin the two properties that matter: the indexes give the SAME answers a scan would,
and they are actually populated rather than being dead weight beside the old loops.
"""

from __future__ import annotations

import datetime as dt

from tigerquant.core.canonical import digest_of
from tigerquant.evidence.facts import Fact, FactKind
from tigerquant.evidence.sources import default_test_registry
from tigerquant.evidence.store import EvidenceStore

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
WIRE = "wire:other"
KEY = ("SPY", "earnings", AT)


def make_fact(*, source_id: str = WIRE, supersedes: str | None = None, **overrides: object) -> Fact:
    """Build a valid Fact. `fact_id` derives from content, so distinct content is distinct."""
    payload: dict[str, object] = {
        "source_id": source_id,
        "kind": FactKind.OBSERVED,
        "category": "earnings",
        "instrument": "SPY",
        "event_time": AT,
        "published_at": AT,
        "available_at": AT,
        "ingested_at": AT,
        "valid_until": AT + dt.timedelta(hours=1),
        "attributes": {"value": "1.00"},
    }
    payload.update(overrides)
    content_hash = digest_of(
        {k: str(v) for k, v in sorted(payload.items()) if k != "source_id"} | {"src": source_id}
    )
    payload["content_hash"] = content_hash
    payload["fact_id"] = f"fact:{content_hash.removeprefix('sha256:')[:24]}"
    payload["supersedes"] = supersedes
    return Fact(**payload)  # type: ignore[arg-type]


def chain_of(length: int) -> tuple[EvidenceStore, list[Fact]]:
    store = EvidenceStore(default_test_registry())
    chain = [make_fact(attributes={"value": "1.00"})]
    store.add(chain[0])
    for index in range(1, length):
        nxt = make_fact(
            attributes={"value": f"1.0{index}"},
            supersedes=chain[-1].fact_id,
        )
        store.add(nxt)
        chain.append(nxt)
    return store, chain


class TestTheSuccessorIndex:
    def test_an_empty_store_has_no_successors(self) -> None:
        assert EvidenceStore(default_test_registry())._successor == {}

    def test_adding_a_revision_records_the_link(self) -> None:
        store, chain = chain_of(2)
        assert store._successor[chain[0].fact_id] == chain[1].fact_id

    def test_a_duplicate_add_does_not_duplicate_the_link(self) -> None:
        store, chain = chain_of(2)
        assert store.add(chain[1]) is False
        assert store._successor[chain[0].fact_id] == chain[1].fact_id

    def test_vintages_walks_a_chain_built_from_the_index(self) -> None:
        store, chain = chain_of(4)
        assert [f.fact_id for f in store.vintages(chain[-1].fact_id)] == [f.fact_id for f in chain]

    def test_vintages_is_discoverable_from_the_middle(self) -> None:
        """A caller may cite any revision, not just the newest."""
        store, chain = chain_of(3)
        assert len(store.vintages(chain[1].fact_id)) == 3

    def test_vintages_of_a_lone_fact_is_itself(self) -> None:
        store, chain = chain_of(1)
        assert store.vintages(chain[0].fact_id) == (chain[0],)

    def test_the_chain_is_oldest_first(self) -> None:
        store, chain = chain_of(3)
        walked = store.vintages(chain[-1].fact_id)
        assert walked[0].fact_id == chain[0].fact_id
        assert walked[-1].fact_id == chain[-1].fact_id

    def test_the_index_is_built_for_facts_passed_to_the_constructor(self) -> None:
        first = make_fact()
        second = make_fact(attributes={"value": "2.00"}, supersedes=first.fact_id)
        store = EvidenceStore(default_test_registry(), facts=[first, second])
        assert store._successor[first.fact_id] == second.fact_id


class TestTheContradictionIndex:
    def test_facts_are_grouped_by_the_key_contradiction_uses(self) -> None:
        store = EvidenceStore(default_test_registry())
        only = make_fact()
        store.add(only)
        assert store._by_key[KEY] == [only.fact_id]

    def test_a_fact_with_no_instrument_is_not_indexed(self) -> None:
        """It can never contradict anything scoped by instrument, so it needs no entry."""
        store = EvidenceStore(default_test_registry())
        store.add(make_fact(instrument=None))
        assert store._by_key == {}

    def test_facts_with_different_event_times_are_separate_groups(self) -> None:
        """Consecutive price bars are not contradictions, and must not be grouped together."""
        store = EvidenceStore(default_test_registry())
        store.add(make_fact())
        later = AT + dt.timedelta(minutes=1)
        store.add(
            make_fact(
                event_time=later,
                published_at=later,
                available_at=later,
                ingested_at=later,
                valid_until=later + dt.timedelta(hours=1),
                attributes={"value": "2"},
            )
        )
        assert len(store._by_key) == 2

    def test_the_index_is_reachable_for_the_key_it_groups_by(self) -> None:
        """The group key must be exactly what `_find_contradictions` looks up."""
        store = EvidenceStore(default_test_registry())
        store.add(make_fact())
        assert KEY in store._by_key
        # A lookup with a key the check would not construct returns nothing, which is the
        # behaviour the index relies on.
        assert store._by_key.get(("QQQ", "earnings", AT), ()) == ()

    def test_a_fact_with_no_value_is_not_indexed(self) -> None:
        """Without a value there is nothing to disagree about."""
        store = EvidenceStore(default_test_registry())
        store.add(make_fact(attributes={}))
        assert store._by_key == {}


class TestBothIndexesStayInStep:
    def test_every_indexed_fact_is_in_the_store(self) -> None:
        """The indexes are maintained in `add()`, the one place a fact enters."""
        store, chain = chain_of(3)
        for fact_id in store._by_key[KEY]:
            assert store.get(fact_id) is not None
        assert len(store) == len(chain)

    def test_the_indexes_do_not_change_what_the_store_reports(self) -> None:
        store, chain = chain_of(3)
        assert len(store) == 3
        assert {f.fact_id for f in store._facts.values()} == {f.fact_id for f in chain}
