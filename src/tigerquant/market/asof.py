"""Point-in-time view — the executable no-lookahead guard.

A strategy never receives the raw bar series. It receives a :class:`PointInTimeView`
whose every accessor is bounded by ``as_of``. Bars become visible at their
``available_at`` (bar close), not at their ``occurred_at`` (bar open), so an indicator
computed from ``closes()`` at 09:31 cannot see the 09:31 bar that closes at 09:32.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.clock import ensure_utc
from tigerquant.core.errors import LookaheadError, MissingMarketData

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from tigerquant.events.market import MarketBar

__all__ = ["PointInTimeView"]


@dataclass(frozen=True, slots=True)
class PointInTimeView:
    """An immutable window over market data.

    Constructed once per decision cycle from everything known so far; visibility is
    computed from ``available_at``, never from wall-clock time or from list position.
    """

    as_of: dt.datetime
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_utc(self.as_of))

    @classmethod
    def empty(cls, as_of: dt.datetime) -> PointInTimeView:
        return cls(as_of=as_of, bars_by_symbol={})

    @property
    def symbols(self) -> tuple[str, ...]:
        """Symbols with at least one visible bar, in insertion order."""
        return tuple(symbol for symbol, bars in self.bars_by_symbol.items() if self._visible(bars))

    def visible_bars(self, symbol: str) -> tuple[MarketBar, ...]:
        """Bars of ``symbol`` that were available at ``as_of``."""
        return self._visible(self.bars_by_symbol.get(symbol, ()))

    def latest_bar(self, symbol: str) -> MarketBar:
        """Most recent visible bar.

        Raises
        ------
        MissingMarketData
            If no bar of ``symbol`` is visible yet. Absence is never filled in with a
            default or an interpolation (acceptance gate 3).
        """
        bars = self.visible_bars(symbol)
        if not bars:
            msg = (
                f"no market data available for {symbol} at {self.as_of.isoformat()}; "
                "a missing price fails closed"
            )
            raise MissingMarketData(msg)
        return bars[-1]

    def require_history(self, symbol: str, count: int) -> tuple[MarketBar, ...]:
        """The most recent ``count`` visible bars, or raise if there are too few."""
        bars = self.visible_bars(symbol)
        if len(bars) < count:
            msg = (
                f"insufficient history for {symbol} at {self.as_of.isoformat()}: "
                f"need {count} bars, have {len(bars)}"
            )
            raise MissingMarketData(msg)
        return bars[-count:]

    def closes(self, symbol: str, count: int) -> tuple[Decimal, ...]:
        """Closing prices of the most recent ``count`` visible bars."""
        return tuple(bar.close for bar in self.require_history(symbol, count))

    def total_visible(self) -> int:
        """Number of visible bars across all symbols."""
        return sum(len(self._visible(bars)) for bars in self.bars_by_symbol.values())

    def with_bars(self, bars: Iterable[MarketBar], as_of: dt.datetime) -> PointInTimeView:
        """Return a new view at ``as_of`` with ``bars`` merged in.

        Bars whose ``available_at`` is after ``as_of`` are kept in the underlying series
        but remain invisible; that lets the guard be tested rather than trusted.
        """
        merged: dict[str, list[MarketBar]] = {
            symbol: list(series) for symbol, series in self.bars_by_symbol.items()
        }
        for bar in bars:
            merged.setdefault(bar.instrument, []).append(bar)
        ordered = {
            symbol: tuple(sorted(series, key=lambda b: (b.available_at, b.seq)))
            for symbol, series in merged.items()
        }
        return PointInTimeView(as_of=as_of, bars_by_symbol=ordered)

    def future_bars(self, symbol: str) -> tuple[MarketBar, ...]:
        """Bars that exist but are *not* yet available — diagnostics only.

        Provided so tests and the ``doctor`` command can prove the boundary exists.
        Any strategy call path that reaches this method is a bug and is caught by the
        usage audit in ``tests/unit/test_no_lookahead.py``.
        """
        return tuple(
            bar for bar in self.bars_by_symbol.get(symbol, ()) if bar.available_at > self.as_of
        )

    def assert_available(self, bar: MarketBar) -> None:
        """Raise :class:`LookaheadError` unless ``bar`` is visible at ``as_of``."""
        if bar.available_at > self.as_of:
            msg = (
                f"lookahead: bar {bar.instrument}@{bar.occurred_at.isoformat()} becomes "
                f"available at {bar.available_at.isoformat()}, after as_of "
                f"{self.as_of.isoformat()}"
            )
            raise LookaheadError(msg)

    def _visible(self, bars: tuple[MarketBar, ...]) -> tuple[MarketBar, ...]:
        return tuple(bar for bar in bars if bar.available_at <= self.as_of)
