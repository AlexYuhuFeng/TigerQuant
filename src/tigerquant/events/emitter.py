"""Event emission.

The emitter is the only place that assigns ``seq`` and ``event_id``, which guarantees a
total order and makes duplicate sequence numbers structurally impossible.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, TypeVar

from tigerquant.events.base import EventBase

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from tigerquant.core.clock import Clock
    from tigerquant.core.enums import Mode
    from tigerquant.core.ids import IdFactory
    from tigerquant.events.journal import JournalRecord, JournalSink

__all__ = ["EventEmitter"]

E = TypeVar("E", bound=EventBase)


class EventEmitter:
    """Builds events with correct envelope fields and appends them to a sink.

    ``observers`` are notified after a durable append, in registration order. That is how
    projections (the order state machine, the risk state) stay in step with the journal
    without the journal needing to know about them.
    """

    __slots__ = ("_clock", "_correlation_id", "_ids", "_mode", "_observers", "_sink")

    def __init__(
        self,
        *,
        mode: Mode,
        sink: JournalSink,
        ids: IdFactory,
        clock: Clock,
        correlation_id: str,
        observers: Sequence[Callable[[EventBase], object]] = (),
    ) -> None:
        self._mode = mode
        self._sink = sink
        self._ids = ids
        self._clock = clock
        self._correlation_id = correlation_id
        self._observers = tuple(observers)

    @property
    def correlation_id(self) -> str:
        return self._correlation_id

    @property
    def mode(self) -> Mode:
        return self._mode

    def emit(
        self,
        model: type[E],
        *,
        occurred_at: dt.datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        **payload: object,
    ) -> E:
        """Create, journal and return an event of ``model`` type."""
        now = self._clock.now()
        event = model(
            event_id=self._ids.next_id("ev"),
            mode=self._mode,
            seq=self._sink.last_seq + 1,
            occurred_at=occurred_at if occurred_at is not None else now,
            recorded_at=now,
            correlation_id=correlation_id or self._correlation_id,
            causation_id=causation_id,
            **payload,
        )
        record: JournalRecord = self._sink.append(event)
        if record.seq != event.seq:
            msg = f"journal returned seq {record.seq} for event seq {event.seq}"
            raise AssertionError(msg)
        for observer in self._observers:
            observer(event)
        return event
