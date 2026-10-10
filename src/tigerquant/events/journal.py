"""Append-only, hash-chained audit journal (ADR-0004).

Record layout on disk — one canonical-JSON object per line::

    {"seq":0,"event_id":"...","event_type":"MarketBar","schema_version":"1.0.0",
     "mode":"BACKTEST","occurred_at":"...","recorded_at":"...",
     "correlation_id":"...","causation_id":null,
     "prev_hash":"sha256:000...0","hash":"sha256:...","payload":{...}}

``hash`` is ``sha256`` over the canonical encoding of the record *without* the
``hash`` field, so any edit to any byte of any earlier line breaks the chain and is
reported by :meth:`AppendOnlyJournal.verify`.

The journal is the primary record; every other store in TigerQuant is a rebuildable
projection of it.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from tigerquant.core.canonical import canonical_json_bytes, sha256_hex
from tigerquant.core.errors import JournalIntegrityError
from tigerquant.events.base import SCHEMA_VERSION, EventBase
from tigerquant.events.registry import parse_event

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tigerquant.core.enums import Mode

__all__ = [
    "GENESIS_PREV_HASH",
    "AppendOnlyJournal",
    "JournalRecord",
    "JournalSink",
    "JournalVerification",
    "MemoryJournal",
]

GENESIS_PREV_HASH = f"sha256:{'0' * 64}"


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One journal line: envelope + chain + payload."""

    seq: int
    event: EventBase
    prev_hash: str
    hash: str

    def body(self) -> dict[str, Any]:
        """The hashed portion of the record (everything except ``hash``)."""
        return {
            **self.event.envelope(),
            "prev_hash": self.prev_hash,
            "payload": self.event.payload(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "hash": self.hash}


@dataclass(frozen=True, slots=True)
class JournalVerification:
    """Outcome of :meth:`AppendOnlyJournal.verify`."""

    ok: bool
    records: int
    first_error: str | None
    first_error_seq: int | None
    digest: str

    def raise_if_invalid(self) -> None:
        if not self.ok:
            msg = f"journal verification failed at seq {self.first_error_seq}: {self.first_error}"
            raise JournalIntegrityError(msg)


class JournalSink(Protocol):
    """Minimal contract the event emitter needs (see ``events/emitter.py``)."""

    @property
    def last_seq(self) -> int:
        """Sequence number of the most recent record, or ``-1`` when empty."""
        ...

    def append(self, event: EventBase) -> JournalRecord:
        """Durably append ``event`` and return the stored record."""
        ...


def _record_hash(body: dict[str, Any]) -> str:
    return f"sha256:{sha256_hex(canonical_json_bytes(body))}"


class MemoryJournal:
    """In-memory :class:`JournalSink` for tests. Same chaining rules as the file sink."""

    __slots__ = ("_records", "mode")

    def __init__(self, mode: Mode) -> None:
        self.mode = mode
        self._records: list[JournalRecord] = []

    @property
    def last_seq(self) -> int:
        return self._records[-1].seq if self._records else -1

    @property
    def records(self) -> tuple[JournalRecord, ...]:
        return tuple(self._records)

    def append(self, event: EventBase) -> JournalRecord:
        if event.mode is not self.mode:
            msg = (
                f"refusing to journal a {event.mode.value} event into a "
                f"{self.mode.value} journal (mode isolation, ADR-0003)"
            )
            raise JournalIntegrityError(msg)
        expected = self.last_seq + 1
        if event.seq != expected:
            msg = f"sequence gap: expected seq {expected}, got {event.seq}"
            raise JournalIntegrityError(msg)
        prev_hash = self._records[-1].hash if self._records else GENESIS_PREV_HASH
        body = {
            **event.envelope(),
            "prev_hash": prev_hash,
            "payload": event.payload(),
        }
        record = JournalRecord(
            seq=event.seq, event=event, prev_hash=prev_hash, hash=_record_hash(body)
        )
        self._records.append(record)
        return record

    def digest(self) -> str:
        """Digest of the whole journal, suitable for equality assertions."""
        return f"sha256:{sha256_hex(canonical_json_bytes([r.to_dict() for r in self._records]))}"


class AppendOnlyJournal:
    """Durable, append-only, hash-chained JSONL journal.

    Opening an existing file resumes the chain: the previous records are verified and
    the next ``seq``/``prev_hash`` continue from the last one, so a restart cannot fork
    the journal.
    """

    __slots__ = ("_fsync", "_handle", "_last_hash", "_last_seq", "_mode", "_path", "_verified")

    def __init__(self, path: Path | str, mode: Mode, *, fsync: bool = True) -> None:
        self._path = Path(path)
        self._mode = mode
        self._fsync = fsync
        self._last_seq = -1
        self._last_hash = GENESIS_PREV_HASH
        self._handle: Any = None
        self._verified: JournalVerification | None = None

    # -- lifecycle ---------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def open(self) -> AppendOnlyJournal:
        """Open for append, verifying any pre-existing content first."""
        if self._handle is not None:
            return self
        if self._path.exists() and self._path.stat().st_size > 0:
            verification = self.verify()
            verification.raise_if_invalid()
            records = list(self.iter_records())
            last = records[-1]
            self._last_seq = last.seq
            self._last_hash = last.hash
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8", newline="\n")
        return self

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None

    def __enter__(self) -> AppendOnlyJournal:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing -----------------------------------------------------------------

    def append(self, event: EventBase) -> JournalRecord:
        """Append ``event`` and flush it to disk before returning."""
        if self._handle is None:
            msg = "journal is not open; use 'with AppendOnlyJournal(...)' or call open()"
            raise JournalIntegrityError(msg)
        if event.mode is not self._mode:
            msg = (
                f"refusing to journal a {event.mode.value} event into a "
                f"{self._mode.value} journal (mode isolation, ADR-0003)"
            )
            raise JournalIntegrityError(msg)
        expected = self._last_seq + 1
        if event.seq != expected:
            msg = f"sequence gap: expected seq {expected}, got {event.seq}"
            raise JournalIntegrityError(msg)

        body = {
            **event.envelope(),
            "prev_hash": self._last_hash,
            "payload": event.payload(),
        }
        record_hash = _record_hash(body)
        record = JournalRecord(
            seq=event.seq, event=event, prev_hash=self._last_hash, hash=record_hash
        )
        self._handle.write(canonical_json_bytes({**body, "hash": record_hash}).decode("utf-8"))
        self._handle.write("\n")
        self._handle.flush()
        if self._fsync:
            os.fsync(self._handle.fileno())

        self._last_seq = event.seq
        self._last_hash = record_hash
        return record

    # -- reading / verifying ------------------------------------------------------

    def iter_records(self) -> Iterator[JournalRecord]:
        """Yield stored records in file order. Raises on malformed or unknown records."""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                import json

                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    msg = f"malformed JSON at line {lineno}: {exc}"
                    raise JournalIntegrityError(msg) from exc
                prev_hash = data.pop("prev_hash", None)
                record_hash = data.pop("hash", None)
                payload = data.pop("payload", {})
                if not isinstance(prev_hash, str) or not isinstance(record_hash, str):
                    msg = f"line {lineno} is missing prev_hash/hash"
                    raise JournalIntegrityError(msg)
                event = parse_event({**data, **payload})
                yield JournalRecord(
                    seq=event.seq,
                    event=event,
                    prev_hash=prev_hash,
                    hash=record_hash,
                )

    def verify(self) -> JournalVerification:
        """Recompute the chain and report the first inconsistency."""
        expected_seq = 0
        expected_prev = GENESIS_PREV_HASH
        count = 0
        for record in self.iter_records():
            if record.seq != expected_seq:
                return JournalVerification(
                    ok=False,
                    records=count,
                    first_error=f"expected seq {expected_seq}, found {record.seq}",
                    first_error_seq=record.seq,
                    digest="",
                )
            if record.prev_hash != expected_prev:
                return JournalVerification(
                    ok=False,
                    records=count,
                    first_error="prev_hash does not match the previous record hash",
                    first_error_seq=record.seq,
                    digest="",
                )
            recomputed = _record_hash(record.body())
            if recomputed != record.hash:
                return JournalVerification(
                    ok=False,
                    records=count,
                    first_error="record hash mismatch (content was altered)",
                    first_error_seq=record.seq,
                    digest="",
                )
            expected_prev = record.hash
            expected_seq += 1
            count += 1
        return JournalVerification(
            ok=True, records=count, first_error=None, first_error_seq=None, digest=self.head_hash()
        )

    def head_hash(self) -> str:
        """Hash of the last record on disk, or the genesis hash when empty."""
        last = GENESIS_PREV_HASH
        for record in self.iter_records():
            last = record.hash
        return last

    def digest(self) -> str:
        """Digest of the entire journal, for equality assertions across runs."""
        return (
            f"sha256:{sha256_hex(canonical_json_bytes([r.to_dict() for r in self.iter_records()]))}"
        )


def default_schema_version() -> str:
    """Exposed for tests that assert the on-disk schema version."""
    return SCHEMA_VERSION
