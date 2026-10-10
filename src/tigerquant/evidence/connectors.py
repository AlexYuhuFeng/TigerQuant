"""Evidence connectors: getting outside text in, safely, and knowing when you cannot.

CR-05's remaining gap. The Market Truth Engine already had a source registry, per-category
staleness, untrusted-text sandboxing, a durable fact log and lineage; what was missing was
anything that actually *fetches*.

Three design decisions carry the safety of this module.

**A connector returns documents, not facts.** :class:`RawDocument` is untrusted text plus
provenance. Turning it into a :class:`~tigerquant.evidence.facts.Fact` — assigning ``fact_id``,
hashing the content, stamping availability — happens in :class:`IngestionPipeline`, in one
place. A connector therefore cannot fabricate a fact id, backdate an availability timestamp, or
mark its own content clean. Anything that can mint a fact can lie about when it was knowable,
and lookahead prevention depends entirely on that timestamp.

**`available_at` is never taken from the source.** A publisher's own timestamp says when they say
they published it. What matters for lookahead is when *we* could first have acted on it, so
``available_at`` is stamped at ingestion from the injected clock. A document whose ``published_at``
claims to be in the future is rejected outright, because that is the fingerprint of either a
clock problem or a manipulated feed.

**A connector that cannot be verified refuses to run.** Market-data entitlements in this project
are **unverified** — there is no licensed feed. :class:`HttpNewsConnector` therefore refuses to
fetch until an entitlement is confirmed in writing, rather than being written hopefully and
disabled by a flag. The fixture connector exists so the whole pipeline is testable offline and
deterministically, which is what CR-05 can honestly deliver today.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import ReasonCode
from tigerquant.core.errors import ConfigurationError, EvidenceError
from tigerquant.evidence.facts import Fact, FactKind, fact_id_for
from tigerquant.evidence.text import sanitise, scan_for_injection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tigerquant.core.clock import Clock
    from tigerquant.evidence.durable import DurableFactStore
    from tigerquant.evidence.sources import Source, SourceRegistry
    from tigerquant.evidence.staleness import StalenessPolicy

__all__ = [
    "FixtureNewsConnector",
    "HttpNewsConnector",
    "IngestionPipeline",
    "IngestionReport",
    "RawDocument",
    "SourceConnector",
]

#: A published timestamp further ahead than this is treated as a data fault, not a scoop.
_MAX_CLOCK_SKEW = dt.timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class RawDocument:
    """Untrusted content plus where it came from. The only thing a connector may produce."""

    source_id: str
    #: The source's own identifier for this document, used for deduplication.
    external_id: str
    title: str
    body: str
    #: What the SOURCE says. Never used as availability; see the module docstring.
    published_at: dt.datetime
    url: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    def describe(self) -> str:
        return f"{self.source_id}:{self.external_id} ({self.published_at.isoformat()})"


@runtime_checkable
class SourceConnector(Protocol):
    """Anything that can produce documents for one registered source.

    Deliberately narrow. A connector has no access to the store, the registry or the clock, so
    it cannot write a fact, cannot decide its own freshness, and cannot mark its own output
    clean. Everything that matters happens in the pipeline.
    """

    @property
    def connector_id(self) -> str: ...

    @property
    def source_id(self) -> str: ...

    def fetch(self, *, since: dt.datetime, until: dt.datetime) -> Sequence[RawDocument]: ...


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """What one ingestion pass did, including what it refused."""

    connector_id: str
    fetched: int
    admitted: int
    duplicates: int
    injection_flagged: int
    #: Admitted facts that SUPERSEDE an earlier revision of the same document.
    revisions: int = 0
    rejections: tuple[tuple[str, str], ...] = ()

    @property
    def rejected(self) -> int:
        return len(self.rejections)

    @property
    def clean(self) -> bool:
        return not self.rejections and not self.injection_flagged

    def summary(self) -> str:
        parts = [
            f"{self.connector_id}: fetched {self.fetched}",
            f"admitted {self.admitted}",
            f"duplicates {self.duplicates}",
        ]
        if self.revisions:
            parts.append(f"revisions {self.revisions}")
        if self.injection_flagged:
            parts.append(f"INJECTION-FLAGGED {self.injection_flagged}")
        if self.rejections:
            parts.append(f"rejected {self.rejected}")
        return ", ".join(parts)

    def as_record(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "fetched": self.fetched,
            "admitted": self.admitted,
            "duplicates": self.duplicates,
            "injection_flagged": self.injection_flagged,
            "revisions": self.revisions,
            "rejected": self.rejected,
            "clean": self.clean,
            "rejections": [
                {"external_id": key, "reason": reason} for key, reason in self.rejections
            ],
        }


class IngestionPipeline:
    """Turns documents from a connector into durable facts, or refuses them.

    The single place where untrusted text becomes a fact. Every step is a check that can fail,
    and a failure means the document is dropped with a reason rather than admitted with a
    caveat.
    """

    __slots__ = ("_clock", "_indexed", "_latest", "_registry", "_staleness", "_store")

    def __init__(
        self,
        *,
        registry: SourceRegistry,
        store: DurableFactStore,
        clock: Clock,
        staleness: StalenessPolicy,
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock
        self._staleness = staleness
        #: (source_id, external_id) -> the newest fact id for that document.
        #:
        #: Built lazily from the store rather than held in memory only, so a pipeline started
        #: against an existing fact log chains onto what is already there instead of treating
        #: every document as new. That matters after a restart, which is exactly when a feed
        #: is most likely to re-serve something.
        self._latest: dict[tuple[str, str], str] = {}
        self._indexed = False

    def ingest(
        self,
        connector: SourceConnector,
        *,
        since: dt.datetime,
        until: dt.datetime,
    ) -> IngestionReport:
        """Fetch, check and store. Returns what happened, including every refusal."""
        source = self._require_registered(connector.source_id)
        now = self._clock.now()
        window_start = ensure_utc(since)
        window_end = ensure_utc(until)

        fetched = 0
        admitted = 0
        duplicates = 0
        flagged = 0
        revisions = 0
        rejections: list[tuple[str, str]] = []

        for document in connector.fetch(since=window_start, until=window_end):
            fetched += 1
            try:
                fact, was_flagged = self._to_fact(document, source=source, now=now)
            except EvidenceError as exc:
                rejections.append((document.external_id, str(exc)))
                continue
            if was_flagged:
                flagged += 1

            # A revision is the same document with different content. Link it to what it
            # replaces BEFORE the store sees it, so the chain is complete the moment the fact
            # exists -- a window where both revisions look current is exactly the situation
            # `supersedes` exists to prevent.
            previous = self._supersedes_for(fact)
            if previous is not None:
                fact = fact.model_copy(update={"supersedes": previous})

            if self._store.add(fact):
                admitted += 1
                if previous is not None:
                    revisions += 1
            else:
                # Already present. Not an error: re-running an ingestion is expected after a
                # crash, and the content hash makes it idempotent.
                duplicates += 1

            # Track the newest revision either way, so a second ingest in the same pass chains
            # onto the right predecessor rather than onto the one from the store.
            external_id = str(fact.attributes.get("external_id", ""))
            if external_id:
                self._latest[(source.source_id, external_id)] = fact.fact_id

        return IngestionReport(
            connector_id=connector.connector_id,
            fetched=fetched,
            admitted=admitted,
            duplicates=duplicates,
            injection_flagged=flagged,
            revisions=revisions,
            rejections=tuple(rejections),
        )

    def _index_existing(self) -> None:
        """Learn which revision of each document is already stored."""
        if self._indexed:
            return
        for fact in self._store.iter_facts():
            external_id = fact.attributes.get("external_id")
            if external_id:
                # Later facts win, and the store yields in append order, so a plain assignment
                # leaves the newest. Revisions are appended, never rewritten.
                self._latest[(fact.source_id, str(external_id))] = fact.fact_id
        self._indexed = True

    def _supersedes_for(self, fact: Fact) -> str | None:
        """The fact this one replaces, or None when it is the first revision.

        A revision is the same DOCUMENT -- same source, same external id -- with different
        CONTENT. Without this, a feed that re-serves an article with a corrected figure would
        leave two facts that both look current, and a snapshot could cite either.
        """
        self._index_existing()
        external_id = str(fact.attributes.get("external_id", ""))
        if not external_id:
            return None
        key = (fact.source_id, external_id)
        previous = self._latest.get(key)
        if previous is None or previous == fact.fact_id:
            return None
        return previous

    # -- the checks --------------------------------------------------------------

    def _require_registered(self, source_id: str) -> Source:
        """A fact may only exist for a registered source, so provenance is not free text."""
        if not self._registry.known(source_id):
            msg = (
                f"source {source_id!r} is not registered. A fact may only be created for a "
                "registered source, so that provenance, reliability and licence scope are "
                "declared rather than asserted by the connector."
            )
            raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED)
        return self._registry.get(source_id)

    def _to_fact(
        self, document: RawDocument, *, source: Source, now: dt.datetime
    ) -> tuple[Fact, bool]:
        """Convert one document into a fact, or raise with the reason it cannot be one."""
        if document.source_id != source.source_id:
            msg = (
                f"the connector returned a document for {document.source_id!r} while registered "
                f"as {source.source_id!r}; a connector may only produce its own source's content"
            )
            raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED)

        published = ensure_utc(document.published_at)
        if published > now + _MAX_CLOCK_SKEW:
            msg = (
                f"published_at {published.isoformat()} is in the future relative to the "
                f"injected clock ({now.isoformat()}). Either a clock is wrong or the feed is "
                "manipulated; both are reasons to drop the document."
            )
            raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED)

        raw_text = document.text
        if not raw_text.strip():
            msg = "the document has no text"
            raise EvidenceError(msg, ReasonCode.EVIDENCE_UNRESOLVED)

        # Sanitise before scanning: control characters and confusables are stripped first so
        # the scan sees the text a reader would.
        # sanitise returns (text, truncated): control characters and confusables are removed
        # so the scan sees what a human reader would, and the result is length-capped.
        cleaned, truncated = sanitise(raw_text)
        scan = scan_for_injection(cleaned)

        # The content hash covers the CLEANED text, so the same document re-fetched with
        # different whitespace is recognised as a duplicate rather than stored twice.
        content_hash = _hash(cleaned, document)

        flags = tuple(scan.flags) if scan.flags else ()
        # The category is the source's KIND. A news wire and an official filing are different
        # categories with different freshness horizons, and the staleness policy is keyed by
        # category, so this is what makes the per-category horizons apply to ingested text.
        category = _category_for(source)
        fact = Fact(
            fact_id=fact_id_for(
                category=category,
                instrument=None,
                source_id=source.source_id,
                event_time=published,
                content_hash=content_hash,
            ),
            # ATTRIBUTED_CLAIM, not OBSERVED. A news article is somebody's assertion; this
            # system did not observe it and must not label it as though it had. That
            # distinction is the whole point of an evidence-grounded trader.
            kind=FactKind.ATTRIBUTED_CLAIM,
            category=category,
            instrument=None,
            # event_time cannot be later than available_at, and the Fact model enforces it --
            # correctly, because a fact whose event precedes our knowledge of it IS lookahead.
            # A source whose clock runs a minute fast is normal, so when that happens the
            # event time is clamped to the moment we learned of it and the source's own claim
            # is preserved separately in published_at and in attributes.
            event_time=min(published, now),
            published_at=published,
            # Stamped at INGESTION, never taken from the source. This is the field that
            # prevents lookahead: what matters is when we could first have acted on it.
            available_at=now,
            ingested_at=now,
            # No expiry is asserted here. Freshness is the staleness policy's decision, made
            # when a snapshot is built, not the connector's.
            valid_until=_staleness_deadline(self._staleness, category, now),
            source_id=source.source_id,
            content_hash=content_hash,
            license_scope=source.license_scope,
            quality_flags=flags,
            supersedes=None,
            attributes=_attributes(
                document, cleaned, published, scan, truncated=truncated, available_at=now
            ),
        )
        return fact, bool(flags)


def _category_for(source: Source) -> str:
    """The staleness category for a source, derived from its declared kind."""
    return str(getattr(source.kind, "value", source.kind)).upper()


def _staleness_deadline(policy: StalenessPolicy, category: str, now: dt.datetime) -> dt.datetime:
    """The instant this fact stops being usable, from the per-category policy."""
    seconds = policy.max_age_seconds(category)
    return now + dt.timedelta(seconds=int(seconds))


def _attributes(
    document: RawDocument,
    cleaned: str,
    published: dt.datetime,
    scan: object,
    *,
    truncated: bool,
    available_at: dt.datetime,
) -> dict[str, str]:
    """Every attribute must be a string: the Fact schema says so, and a mixed-type bag would
    make the canonical form depend on JSON coercion."""
    matched = getattr(scan, "matched", ()) or ()
    attributes: dict[str, str] = {
        "title": sanitise(document.title)[0],
        "text": cleaned,
        "url": document.url,
        "external_id": document.external_id,
        "published_at": published.isoformat(),
    }
    if published > available_at:
        # The source claimed a time we had not reached yet. Within tolerance that is clock
        # skew rather than manipulation, but it is still worth recording.
        attributes["published_ahead_of_ingestion"] = published.isoformat()
    if truncated:
        # A truncated document is a partial one, and saying so matters: a fact holding the
        # first N characters of an article supports a different claim from the article.
        attributes["truncated"] = "true"
    if matched:
        attributes["injection_patterns"] = ",".join(str(item) for item in matched)
    for key, value in document.attributes.items():
        attributes[str(key)] = str(value)
    return attributes


def _hash(cleaned: str, document: RawDocument) -> str:
    from tigerquant.core.canonical import digest_of

    return digest_of(
        {"source": document.source_id, "external_id": document.external_id, "text": cleaned.strip()}
    )


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FixtureNewsConnector:
    """A deterministic connector over documents supplied in code.

    The point of this one is that the whole ingestion path — registry, sanitising, injection
    scanning, hashing, staleness, durable storage, lineage — is exercisable offline and
    reproducibly. It is labelled ``SYNTHETIC`` where it is used, and it is not a news feed.
    """

    source_id: str
    documents: tuple[RawDocument, ...]
    connector_id: str = "fixture-news"

    def fetch(self, *, since: dt.datetime, until: dt.datetime) -> tuple[RawDocument, ...]:
        """Return documents in the window, oldest first.

        A real connector would page. This one filters, and the ordering is deliberate: the
        pipeline stores facts in the order it receives them, so a deterministic order makes the
        durable log reproducible.
        """
        start = ensure_utc(since)
        end = ensure_utc(until)
        selected = [
            document
            for document in self.documents
            if start <= ensure_utc(document.published_at) <= end
        ]
        return tuple(sorted(selected, key=lambda item: (item.published_at, item.external_id)))


@dataclass(frozen=True, slots=True)
class HttpNewsConnector:
    """A real feed connector that REFUSES to run until an entitlement is confirmed.

    Market-data entitlements in this project are **unverified**: there is no licensed feed, no
    vendor confirmation, and no reviewed redistribution right. A connector that fetched anyway
    would be both a licence problem and a lie about readiness, so this one raises instead.

    The refusal is the deliverable. What a licensed deployment would need is: an entitlement
    reference (so the licence is a recorded fact rather than an assumption), a fetcher, and a
    registered source whose ``license_scope`` matches the entitlement. The fetcher is not
    written, because a fetcher that has never run against a real endpoint is untested code
    wearing a working label (AGENTS.md invariant 11).
    """

    source_id: str
    endpoint: str
    #: Written confirmation that this feed may be used. Empty means "not confirmed".
    entitlement_reference: str = ""
    connector_id: str = "http-news"

    def fetch(self, *, since: dt.datetime, until: dt.datetime) -> tuple[RawDocument, ...]:
        del since, until
        if not self.entitlement_reference:
            msg = (
                f"refusing to fetch {self.endpoint!r}: no market-data entitlement has been "
                "confirmed for this feed. Entitlements are a commercial fact, not a code fact, "
                "and they are currently unverified in this project (see docs/LIVE_READINESS.md, "
                "precondition 3). Set entitlement_reference to the written confirmation before "
                "this connector will run."
            )
            raise ConfigurationError(msg)
        msg = (
            "this connector has no fetcher. It is a documented placeholder for a licensed "
            "deployment, and it says so rather than returning an empty result that would look "
            "like a working feed with no news."
        )
        raise ConfigurationError(msg)


def reason_for_rejection(message: str) -> ReasonCode:
    """Map a rejection message onto a reason code, for the audit record."""
    lowered = message.lower()
    if "not registered" in lowered:
        return ReasonCode.EVIDENCE_UNRESOLVED
    if "future" in lowered:
        return ReasonCode.EVIDENCE_STALE
    if "no text" in lowered:
        return ReasonCode.EVIDENCE_UNRESOLVED
    return ReasonCode.CHECK_ERROR


def window_for(now: dt.datetime, *, minutes: int = 60) -> tuple[dt.datetime, dt.datetime]:
    """A fetch window ending at ``now``."""
    end = ensure_utc(now)
    return end - dt.timedelta(minutes=minutes), end
