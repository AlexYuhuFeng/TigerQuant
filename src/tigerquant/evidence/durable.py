"""Durable, append-only fact log.

The evidence store is the system's memory of *why* a decision was allowed. Keeping it only
in RAM would mean a restart silently erases the provenance of every past decision, so the
durable store is an append-only JSONL log with the same hash-chain discipline as the audit
journal (ADR-0004), one record per fact::

    {"fact": {...}, "prev_hash": "sha256:...", "hash": "sha256:..."}

Guarantees:

* **Append-only.** Facts are never rewritten. A correction is a *new* fact that
  ``supersedes`` the old one, so a replay at an earlier instant still sees what was known
  then (vintage correctness, acceptance gate 12).
* **Tamper-evident.** Every record commits to its predecessor; :meth:`verify` recomputes
  the chain and re-validates every fact through pydantic.
* **Idempotent ingestion.** Re-ingesting a fact already in the log is a no-op, so a retried
  feed poll cannot duplicate evidence.
* **Provenance-enforced.** A fact whose source is not registered cannot be written.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import canonical_json_bytes, sha256_hex
from tigerquant.core.errors import JournalIntegrityError
from tigerquant.evidence.facts import Fact

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tigerquant.evidence.sources import SourceRegistry
    from tigerquant.evidence.store import EvidenceStore

__all__ = ["FACT_LOG_GENESIS", "DurableFactStore", "FactChainVerification"]

FACT_LOG_GENESIS = f"sha256:{'0' * 64}"


@dataclass(frozen=True, slots=True)
class FactChainVerification:
    """Outcome of verifying a durable fact log."""

    ok: bool
    records: int
    first_error: str | None
    first_error_index: int | None
    head_hash: str

    def raise_if_invalid(self) -> None:
        if not self.ok:
            msg = (
                f"evidence log verification failed at record {self.first_error_index}: "
                f"{self.first_error}"
            )
            raise JournalIntegrityError(msg)


def _record_hash(body: dict[str, Any]) -> str:
    return f"sha256:{sha256_hex(canonical_json_bytes(body))}"


class DurableFactStore:
    """Append-only fact log plus the in-memory point-in-time view over it."""

    __slots__ = ("_fsync", "_handle", "_head", "_path", "_registry", "_store")

    def __init__(
        self,
        path: Path | str,
        *,
        registry: SourceRegistry,
        store: EvidenceStore,
        fsync: bool = True,
    ) -> None:
        self._path = Path(path)
        self._registry = registry
        self._store = store
        self._fsync = fsync
        self._handle: Any = None
        self._head = FACT_LOG_GENESIS

    # -- lifecycle ---------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def head_hash(self) -> str:
        return self._head

    @property
    def store(self) -> EvidenceStore:
        return self._store

    def open(self) -> DurableFactStore:
        """Open for append, verifying and loading any existing content first."""
        if self._handle is not None:
            return self
        if self._path.exists() and self._path.stat().st_size > 0:
            verification = self.verify()
            verification.raise_if_invalid()
            for fact in self.iter_facts():
                self._store.add(fact)
            self._head = verification.head_hash
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8", newline="\n")
        return self

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None

    def __enter__(self) -> DurableFactStore:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing -----------------------------------------------------------------

    def add(self, fact: Fact) -> bool:
        """Persist ``fact``. Returns ``False`` when it was already present."""
        if self._handle is None:
            msg = "fact log is not open; use 'with DurableFactStore(...)' or call open()"
            raise JournalIntegrityError(msg)
        self._registry.get(fact.source_id)
        if not self._store.add(fact):
            return False
        body = {"fact": fact.model_dump(mode="python"), "prev_hash": self._head}
        record_hash = _record_hash(body)
        self._handle.write(canonical_json_bytes({**body, "hash": record_hash}).decode("utf-8"))
        self._handle.write("\n")
        self._handle.flush()
        if self._fsync:
            os.fsync(self._handle.fileno())
        self._head = record_hash
        return True

    def extend(self, facts: Iterator[Fact] | list[Fact]) -> int:
        return sum(1 for fact in facts if self.add(fact))

    # -- reading / verifying ------------------------------------------------------

    def iter_facts(self) -> Iterator[Fact]:
        """Yield facts in file order, validating each one."""
        if not self._path.exists():
            return
        import json

        with self._path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    msg = f"malformed JSON at record {index}: {exc}"
                    raise JournalIntegrityError(msg) from exc
                payload = data.get("fact")
                if not isinstance(payload, dict):
                    msg = f"record {index} has no 'fact' object"
                    raise JournalIntegrityError(msg)
                try:
                    yield Fact.model_validate(payload)
                except Exception as exc:
                    msg = f"record {index} is not a valid fact: {exc}"
                    raise JournalIntegrityError(msg) from exc

    def verify(self) -> FactChainVerification:
        """Recompute the chain and re-validate every fact."""
        import json

        if not self._path.exists():
            return FactChainVerification(True, 0, None, None, FACT_LOG_GENESIS)

        expected_prev = FACT_LOG_GENESIS
        count = 0
        with self._path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    return FactChainVerification(
                        False, count, f"malformed JSON: {exc}", index, expected_prev
                    )
                record_hash = data.pop("hash", None)
                prev_hash = data.get("prev_hash")
                if not isinstance(record_hash, str) or not isinstance(prev_hash, str):
                    return FactChainVerification(
                        False, count, "record is missing hash/prev_hash", index, expected_prev
                    )
                if prev_hash != expected_prev:
                    return FactChainVerification(
                        False,
                        count,
                        "prev_hash does not match the previous record",
                        index,
                        expected_prev,
                    )
                if _record_hash(data) != record_hash:
                    return FactChainVerification(
                        False,
                        count,
                        "record hash mismatch (content was altered)",
                        index,
                        expected_prev,
                    )
                try:
                    Fact.model_validate(data["fact"])
                except Exception as exc:  # noqa: BLE001 - any parse failure means invalid
                    return FactChainVerification(
                        False, count, f"invalid fact: {exc}", index, expected_prev
                    )
                expected_prev = record_hash
                count += 1
        return FactChainVerification(True, count, None, None, expected_prev)

    def __len__(self) -> int:
        return sum(1 for _ in self.iter_facts())
