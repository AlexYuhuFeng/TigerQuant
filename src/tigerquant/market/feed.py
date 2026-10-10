"""Deterministic synthetic market feed.

Turns a :class:`~tigerquant.market.fixtures.ScenarioFixture` into an ordered schedule of
bar drafts. The schedule order is ``(offset, universe index)`` — a total order derived
from the fixture — so the emitted event stream is identical on every run and on every
machine (ADR-0011 §3).

A bar's ``available_at`` is its **close** time, not its open time: that is the earliest
instant a decision could legitimately have known the value.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from tigerquant.core.canonical import digest_of
from tigerquant.core.enums import Interval
from tigerquant.events.base import AwareUTC

if TYPE_CHECKING:
    from tigerquant.market.fixtures import BarSpec, ScenarioFixture

__all__ = ["BarDraft", "DeterministicMarketFeed", "FeedStep", "interval_delta"]

_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "1d": 1440,
}


def interval_delta(interval: Interval) -> dt.timedelta:
    """Duration of one bar of ``interval``."""
    return dt.timedelta(minutes=_INTERVAL_MINUTES[interval.value])


@dataclass(frozen=True, slots=True)
class FeedStep:
    """One scheduled bar, in the fixture's total order."""

    index: int
    symbol: str
    bar: BarSpec


class BarDraft(BaseModel):
    """Payload of a :class:`~tigerquant.events.market.MarketBar`, before sequencing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: str
    interval: Interval
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    occurred_at: AwareUTC
    available_at: AwareUTC
    source_id: str
    content_hash: str
    reference_price: Decimal


class DeterministicMarketFeed:
    """Emits synthetic bars from a fixture, with provenance."""

    __slots__ = ("_scenario", "_universe_index")

    def __init__(self, scenario: ScenarioFixture) -> None:
        self._scenario = scenario
        self._universe_index = {symbol: index for index, symbol in enumerate(scenario.universe)}

    @property
    def scenario(self) -> ScenarioFixture:
        return self._scenario

    def schedule(self) -> tuple[FeedStep, ...]:
        """All bars of all instruments in a deterministic total order."""
        steps: list[FeedStep] = []
        for symbol in self._scenario.universe:
            instrument = self._scenario.instruments[symbol]
            for bar in instrument.bars:
                steps.append(FeedStep(index=0, symbol=symbol, bar=bar))
        steps.sort(key=lambda step: (step.bar.t, self._universe_index[step.symbol]))
        return tuple(
            FeedStep(index=index, symbol=step.symbol, bar=step.bar)
            for index, step in enumerate(steps)
        )

    def draft(self, step: FeedStep) -> BarDraft:
        """Build the bar payload for ``step``, including its content digest."""
        start = self._scenario.start
        occurred_at = step.bar.timestamp(start)
        available_at = occurred_at + interval_delta(self._scenario.interval)
        source_id = f"fixture:{self._scenario.scenario_id}"
        content_hash = digest_of(
            {
                "scenario": self._scenario.scenario_id,
                "symbol": step.symbol,
                "t": step.bar.t,
                "o": step.bar.o,
                "h": step.bar.h,
                "l": step.bar.l,
                "c": step.bar.c,
                "v": step.bar.v,
            }
        )
        return BarDraft(
            instrument=step.symbol,
            interval=self._scenario.interval,
            open=step.bar.o,
            high=step.bar.h,
            low=step.bar.l,
            close=step.bar.c,
            volume=step.bar.v,
            occurred_at=occurred_at,
            available_at=available_at,
            source_id=source_id,
            content_hash=content_hash,
            reference_price=step.bar.c,
        )

    def drafts(self) -> tuple[BarDraft, ...]:
        """All bar payloads in schedule order (used by tests and offline tooling)."""
        return tuple(self.draft(step) for step in self.schedule())
