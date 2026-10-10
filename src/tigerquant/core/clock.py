"""Injected time.

Production code never reads the wall clock directly: it asks a :class:`Clock`. This is
what makes an end-to-end replay byte-identical across runs (ADR-0011) and what lets a
test advance time deterministically instead of sleeping.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

__all__ = ["UTC", "Clock", "SimulatedClock", "SystemClock", "ensure_utc"]

UTC = dt.UTC


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    Naive datetimes are **rejected**: an unqualified timestamp in an audit record is a
    correctness bug, not a formatting preference.
    """
    if value.tzinfo is None:
        msg = f"naive datetime is not allowed: {value!r}"
        raise ValueError(msg)
    return value.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """A source of time."""

    def now(self) -> dt.datetime:
        """Current instant, timezone-aware UTC."""
        ...


class SystemClock:
    """Real time. Used by paper/shadow workers and by tests that assert on latency."""

    __slots__ = ()

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=UTC)

    def __repr__(self) -> str:
        return "SystemClock()"


class SimulatedClock:
    """Time that only moves when told to.

    Starts at ``start`` and never advances on its own, so two runs of the same
    scenario observe identical timestamps.
    """

    __slots__ = ("_now",)

    def __init__(self, start: dt.datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> dt.datetime:
        return self._now

    def advance_to(self, instant: dt.datetime) -> None:
        """Move to ``instant``. Moving backwards is rejected (monotonicity)."""
        target = ensure_utc(instant)
        if target < self._now:
            msg = f"simulated clock cannot move backwards: {self._now.isoformat()} -> {target.isoformat()}"
            raise ValueError(msg)
        self._now = target

    def advance(self, delta: dt.timedelta) -> None:
        if delta < dt.timedelta(0):
            msg = f"simulated clock cannot advance by a negative delta: {delta!r}"
            raise ValueError(msg)
        self._now += delta

    def __repr__(self) -> str:
        return f"SimulatedClock({self._now.isoformat()})"
