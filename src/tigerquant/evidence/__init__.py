"""Market Truth Engine: typed facts, point-in-time store, frozen snapshots.

CR-01 provides the interface and a deterministic in-memory implementation. CR-05 adds
durable storage, news/filing ingestion, licensing scope handling and historical vintage
replay against the same interface (ADR-0005).
"""

from __future__ import annotations

from tigerquant.evidence.facts import KNOWN_CATEGORIES, Fact, FactKind, fact_id_for
from tigerquant.evidence.ingest import fact_from_bar, fact_from_quote
from tigerquant.evidence.snapshot import EvidenceSnapshot, SnapshotBuilder
from tigerquant.evidence.store import EvidenceStore, Resolution, ResolutionReport

__all__ = [
    "KNOWN_CATEGORIES",
    "EvidenceSnapshot",
    "EvidenceStore",
    "Fact",
    "FactKind",
    "Resolution",
    "ResolutionReport",
    "SnapshotBuilder",
    "fact_from_bar",
    "fact_from_quote",
    "fact_id_for",
]
