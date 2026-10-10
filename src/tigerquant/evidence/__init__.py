"""Market Truth Engine: typed facts, provenance, point-in-time store, frozen snapshots.

CR-01 fixed the interfaces; CR-05 adds durable storage, a source registry, per-category
staleness, prompt-injection-resistant untrusted text, revision/vintage handling and the
claim-to-evidence graph for derived facts (ADR-0005).
"""

from __future__ import annotations

from tigerquant.evidence.durable import (
    FACT_LOG_GENESIS,
    DurableFactStore,
    FactChainVerification,
)
from tigerquant.evidence.facts import KNOWN_CATEGORIES, Fact, FactKind, fact_id_for
from tigerquant.evidence.ingest import fact_from_bar, fact_from_quote
from tigerquant.evidence.lineage import (
    CALCULATION_VERSION,
    DERIVED_SOURCE_ID,
    DerivedFeature,
    derive_fact,
    effective_max_age,
    momentum_feature,
)
from tigerquant.evidence.snapshot import EvidenceSnapshot, SnapshotBuilder
from tigerquant.evidence.sources import (
    DEFAULT_SOURCES,
    LicenseScope,
    Reliability,
    Source,
    SourceKind,
    SourceRegistry,
)
from tigerquant.evidence.staleness import DEFAULT_STALENESS, StalenessPolicy
from tigerquant.evidence.store import (
    INJECTION_FLAG_PREFIX,
    EvidenceStore,
    Resolution,
    ResolutionReport,
    ResolutionStatus,
)
from tigerquant.evidence.text import (
    INJECTION_PATTERNS,
    InjectionScan,
    UntrustedText,
    sanitise,
    scan_for_injection,
)

__all__ = [
    "CALCULATION_VERSION",
    "DEFAULT_SOURCES",
    "DEFAULT_STALENESS",
    "DERIVED_SOURCE_ID",
    "FACT_LOG_GENESIS",
    "INJECTION_FLAG_PREFIX",
    "INJECTION_PATTERNS",
    "KNOWN_CATEGORIES",
    "DerivedFeature",
    "DurableFactStore",
    "EvidenceSnapshot",
    "EvidenceStore",
    "Fact",
    "FactChainVerification",
    "FactKind",
    "InjectionScan",
    "LicenseScope",
    "Reliability",
    "Resolution",
    "ResolutionReport",
    "ResolutionStatus",
    "SnapshotBuilder",
    "Source",
    "SourceKind",
    "SourceRegistry",
    "StalenessPolicy",
    "UntrustedText",
    "derive_fact",
    "effective_max_age",
    "fact_from_bar",
    "fact_from_quote",
    "fact_id_for",
    "momentum_feature",
    "sanitise",
    "scan_for_injection",
]
