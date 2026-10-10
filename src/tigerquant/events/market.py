"""Market-data events.

Provenance is not optional: a market event carries the source identity, the content
digest, and the five timestamps described in ADR-0005. ``available_at`` is the earliest
instant the system could legitimately have known the value and is what the as-of view
(``market.asof``) filters on — that is the formal lookahead guard.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from tigerquant.core.enums import Interval
from tigerquant.events.base import AwareUTC, EventBase, NonEmptyStr, NonNegativeInt, PositiveDecimal

__all__ = ["MarketBar", "MarketQuote"]


class MarketBar(EventBase):
    """One OHLCV bar for one instrument."""

    event_type: Literal["MarketBar"] = "MarketBar"

    instrument: NonEmptyStr
    interval: Interval
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeInt

    #: Earliest instant this bar could legitimately be used by a decision.
    available_at: AwareUTC
    #: Source that produced the value (e.g. ``fixture:trend_up``).
    source_id: NonEmptyStr
    #: sha256 of the canonical source record, for lineage.
    content_hash: NonEmptyStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    #: Value used by deterministic runs (windowed VWAP or close), computed by the feed.
    reference_price: PositiveDecimal

    def __str__(self) -> str:
        return (
            f"MarketBar({self.instrument} {self.interval} {self.occurred_at.isoformat()} "
            f"O={self.open} H={self.high} L={self.low} C={self.close} V={self.volume})"
        )


class MarketQuote(EventBase):
    """Top-of-book quote. Used for marks and for staleness checks."""

    event_type: Literal["MarketQuote"] = "MarketQuote"

    instrument: NonEmptyStr
    bid: Decimal
    ask: Decimal
    available_at: AwareUTC
    source_id: NonEmptyStr
    content_hash: NonEmptyStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2
