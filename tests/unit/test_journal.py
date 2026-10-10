"""Append-only journal: chain integrity, tamper detection, mode isolation, resume."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Mode
from tigerquant.core.errors import JournalIntegrityError
from tigerquant.core.ids import IdFactory
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.journal import GENESIS_PREV_HASH, AppendOnlyJournal, MemoryJournal
from tigerquant.events.market import MarketBar

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


def _bar(emitter: EventEmitter, close: str = "600.50") -> MarketBar:
    return emitter.emit(
        MarketBar,
        occurred_at=AT,
        instrument="SPY",
        interval="1m",
        open="600.00",
        high="601.00",
        low="599.50",
        close=close,
        volume=1000,
        available_at=AT + dt.timedelta(minutes=1),
        source_id="fixture:test",
        content_hash="sha256:" + "a" * 64,
        reference_price=close,
    )


def _emitter(
    sink: object, clock: SimulatedClock, ids: IdFactory, mode: Mode = Mode.BACKTEST
) -> EventEmitter:
    return EventEmitter(mode=mode, sink=sink, ids=ids, clock=clock, correlation_id="test")  # type: ignore[arg-type]


class TestMemoryJournal:
    def test_genesis_prev_hash_is_used_for_the_first_record(self) -> None:
        clock = SimulatedClock(AT)
        journal = MemoryJournal(Mode.BACKTEST)
        _bar(_emitter(journal, clock, IdFactory("bt", clock)))
        assert journal.records[0].prev_hash == GENESIS_PREV_HASH

    def test_sequence_gap_is_rejected(self) -> None:
        clock = SimulatedClock(AT)
        journal = MemoryJournal(Mode.BACKTEST)
        emitter = _emitter(journal, clock, IdFactory("bt", clock))
        bar = _bar(emitter)
        # Forge a copy with a jumped sequence number.
        forged = bar.model_copy(update={"seq": 5, "event_id": "bt-ev_forged"})
        with pytest.raises(JournalIntegrityError, match="sequence gap"):
            journal.append(forged)

    def test_mode_isolation(self) -> None:
        clock = SimulatedClock(AT)
        journal = MemoryJournal(Mode.PAPER)
        emitter = _emitter(journal, clock, IdFactory("bt", clock), mode=Mode.BACKTEST)
        with pytest.raises(JournalIntegrityError, match="mode isolation"):
            _bar(emitter)


class TestAppendOnlyJournal:
    def test_roundtrip_and_verify(self, tmp_path: Path) -> None:
        clock = SimulatedClock(AT)
        path = tmp_path / "audit.jsonl"
        with AppendOnlyJournal(path, Mode.BACKTEST) as journal:
            emitter = _emitter(journal, clock, IdFactory("bt", clock))
            _bar(emitter)
            _bar(emitter, close="601.50")
        verification = AppendOnlyJournal(path, Mode.BACKTEST).verify()
        assert verification.ok
        assert verification.records == 2

    def test_reopen_resumes_the_chain(self, tmp_path: Path) -> None:
        clock = SimulatedClock(AT)
        path = tmp_path / "audit.jsonl"
        with AppendOnlyJournal(path, Mode.BACKTEST) as journal:
            emitter = _emitter(journal, clock, IdFactory("bt", clock))
            _bar(emitter)
            first_head = journal.head_hash()
        with AppendOnlyJournal(path, Mode.BACKTEST) as journal:
            assert journal.last_seq == 0
            emitter = _emitter(journal, clock, IdFactory("bt", clock))
            _bar(emitter, close="602.50")
            assert journal.last_seq == 1
        verification = AppendOnlyJournal(path, Mode.BACKTEST).verify()
        assert verification.ok
        assert verification.records == 2
        assert first_head != verification.digest

    def test_tampering_with_a_payload_is_detected(self, tmp_path: Path) -> None:
        clock = SimulatedClock(AT)
        path = tmp_path / "audit.jsonl"
        with AppendOnlyJournal(path, Mode.BACKTEST) as journal:
            emitter = _emitter(journal, clock, IdFactory("bt", clock))
            _bar(emitter)
            _bar(emitter, close="601.50")
        text = path.read_text(encoding="utf-8")
        tampered = text.replace('"volume":1000', '"volume":9999')
        assert tampered != text, "test precondition: the volume field must be present"
        path.write_text(tampered, encoding="utf-8")

        verification = AppendOnlyJournal(path, Mode.BACKTEST).verify()
        assert not verification.ok
        assert "altered" in (verification.first_error or "")

    def test_tampering_with_an_earlier_record_breaks_the_chain(self, tmp_path: Path) -> None:
        clock = SimulatedClock(AT)
        path = tmp_path / "audit.jsonl"
        with AppendOnlyJournal(path, Mode.BACKTEST) as journal:
            emitter = _emitter(journal, clock, IdFactory("bt", clock))
            _bar(emitter)
            _bar(emitter, close="601.50")
            _bar(emitter, close="602.50")
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace('"volume":1000', '"volume":1234')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        verification = AppendOnlyJournal(path, Mode.BACKTEST).verify()
        assert not verification.ok
        assert verification.first_error_seq == 0

    def test_reopening_a_corrupt_journal_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text('{"not": "a journal record"}\n', encoding="utf-8")
        with pytest.raises(JournalIntegrityError):
            AppendOnlyJournal(path, Mode.BACKTEST).open()

    def test_unknown_event_type_is_a_hard_error(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text(
            '{"seq":0,"event_id":"x","event_type":"NotAnEvent","schema_version":"1.0.0",'
            '"mode":"BACKTEST","occurred_at":"2026-03-02T14:40:00.000000+00:00",'
            '"recorded_at":"2026-03-02T14:40:00.000000+00:00","correlation_id":"c",'
            '"causation_id":null,"prev_hash":"sha256:'
            + "0" * 64
            + '","hash":"sha256:'
            + "1" * 64
            + '","payload":{}}\n',
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="unknown event_type"):
            AppendOnlyJournal(path, Mode.BACKTEST).verify()
