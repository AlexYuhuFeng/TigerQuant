"""Source registry and provenance classes.

A fact may only be created for a **registered** source. That single rule is what makes
provenance mean something: without it, ``source_id`` is a free-text field and the
claim-to-evidence graph degrades into labels. Ingestion resolves the source through this
registry, and an unknown source is a hard error rather than a new implicit source.

Three orthogonal properties are recorded per source:

* :class:`SourceKind` — what kind of thing it is (an exchange feed, a filing, a wire);
* :class:`Reliability` — how much weight a *derived* rule may place on it;
* :class:`LicenseScope` — whether the content may leave the system at all (ADR-0005 §7).
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from tigerquant.core.enums import ReasonCode
from tigerquant.core.errors import EvidenceError

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "DEFAULT_SOURCES",
    "TEST_SOURCES",
    "LicenseScope",
    "Reliability",
    "Source",
    "SourceKind",
    "SourceRegistry",
    "default_test_registry",
]


class SourceKind(StrEnum):
    """What produced the observation."""

    #: A licensed or public exchange/market-data feed (prices, quotes).
    MARKET_DATA = "MARKET_DATA"
    #: The broker (positions, balances, order state).
    BROKER = "BROKER"
    #: A regulator or issuer filing (SEC EDGAR, company IR).
    OFFICIAL_FILING = "OFFICIAL_FILING"
    #: A news wire or publisher.
    NEWS_WIRE = "NEWS_WIRE"
    #: Computed by us from other facts. Lineage is mandatory.
    DERIVED = "DERIVED"
    #: A synthetic fixture. Never leaves the test/backtest world.
    SYNTHETIC = "SYNTHETIC"


class Reliability(IntEnum):
    """How much weight a derived rule may place on a source.

    This is *not* a truth claim about any market theory — it ranks attribution quality
    only (ADR-0005 §1).
    """

    #: Unattributed, unverified, or synthetic.
    UNVERIFIED = 1
    #: A named publisher with a track record, but no primary-source confirmation.
    REPUTABLE = 2
    #: The primary record itself: the exchange, the regulator, the issuer, the broker.
    AUTHORITATIVE = 3


class LicenseScope(StrEnum):
    """Redistribution rules attached to content (ADR-0005 §7)."""

    #: Freely redistributable.
    PUBLIC = "PUBLIC"
    #: Usable inside the system; must not appear in exports or dashboards.
    INTERNAL = "INTERNAL"
    #: Must not be stored beyond the decision window, and never exported.
    RESTRICTED = "RESTRICTED"

    def may_export(self) -> bool:
        return self is LicenseScope.PUBLIC


class Source(BaseModel):
    """A registered origin of observations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9:._\-]*$")]
    kind: SourceKind
    publisher: Annotated[str, Field(min_length=1)]
    reliability: Reliability
    license_scope: LicenseScope = LicenseScope.INTERNAL
    notes: str = ""

    def is_authoritative(self) -> bool:
        return self.reliability is Reliability.AUTHORITATIVE


#: Sources the offline slice is allowed to use. Everything else must be registered
#: explicitly by a deployment.
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source(
        source_id="fixture:synthetic",
        kind=SourceKind.SYNTHETIC,
        publisher="TigerQuant test fixtures",
        reliability=Reliability.UNVERIFIED,
        license_scope=LicenseScope.PUBLIC,
        notes="Deterministic synthetic bars. Never indicates real market behaviour.",
    ),
    Source(
        source_id="derived:local",
        kind=SourceKind.DERIVED,
        publisher="TigerQuant deterministic services",
        reliability=Reliability.AUTHORITATIVE,
        license_scope=LicenseScope.PUBLIC,
        notes="Computed by us from other facts; lineage is mandatory.",
    ),
)


class SourceRegistry:
    """The set of sources a deployment is allowed to ingest from."""

    __slots__ = ("_sources",)

    def __init__(self, sources: Iterable[Source] = DEFAULT_SOURCES) -> None:
        registry: dict[str, Source] = {}
        for source in sources:
            if source.source_id in registry:
                msg = f"duplicate source registration: {source.source_id}"
                raise ValueError(msg)
            registry[source.source_id] = source
        self._sources = registry

    def register(self, source: Source) -> None:
        """Add a source. Re-registering the same id is an error, not an overwrite."""
        if source.source_id in self._sources:
            msg = f"source {source.source_id!r} is already registered"
            raise ValueError(msg)
        self._sources[source.source_id] = source

    def known(self, source_id: str) -> bool:
        return source_id in self._sources

    def get(self, source_id: str) -> Source:
        """Look up a source, failing closed when it is unknown."""
        try:
            return self._sources[source_id]
        except KeyError as exc:
            msg = (
                f"source {source_id!r} is not registered; a fact may only be recorded for a "
                "known source (provenance is not free text)"
            )
            raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED) from exc

    def source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))

    def as_tuple(self) -> tuple[Source, ...]:
        return tuple(self._sources[key] for key in sorted(self._sources))

    def exportable(self) -> tuple[Source, ...]:
        return tuple(source for source in self.as_tuple() if source.license_scope.may_export())


#: Synthetic sources the test suite and offline fixtures are allowed to use.
TEST_SOURCES: tuple[Source, ...] = (
    Source(
        source_id="fixture:test",
        kind=SourceKind.SYNTHETIC,
        publisher="TigerQuant test suite",
        reliability=Reliability.UNVERIFIED,
        license_scope=LicenseScope.PUBLIC,
    ),
    Source(
        source_id="wire:other",
        kind=SourceKind.NEWS_WIRE,
        publisher="Test wire",
        reliability=Reliability.REPUTABLE,
        license_scope=LicenseScope.INTERNAL,
    ),
)


def default_test_registry() -> SourceRegistry:
    """A registry containing the default sources plus the test-only synthetic ones."""
    return SourceRegistry((*DEFAULT_SOURCES, *TEST_SOURCES))
