"""Turning observations into facts.

The conversion is pure and deterministic: the same bar always yields the same
``fact_id`` and the same attributes, so a replay reconstructs an identical evidence
store (and therefore an identical snapshot digest).
"""

from __future__ import annotations

import datetime as dt

from tigerquant.core.money import dec_str
from tigerquant.events.market import MarketBar, MarketQuote
from tigerquant.evidence.facts import Fact, FactKind, fact_id_for, price_attributes

__all__ = ["fact_from_bar", "fact_from_quote"]


def fact_from_bar(bar: MarketBar, *, max_age_seconds: int) -> Fact:
    """An ``OBSERVED`` price fact for one bar.

    ``ingested_at`` equals ``available_at``: in a deterministic replay we "learn" a bar
    the instant it closes. In a real feed ingestion lags, and the resulting difference is
    what the staleness gate measures.
    """
    if max_age_seconds < 1:
        msg = "max_age_seconds must be positive"
        raise ValueError(msg)
    return Fact(
        fact_id=fact_id_for(
            category="price_bar",
            instrument=bar.instrument,
            source_id=bar.source_id,
            event_time=bar.occurred_at,
            content_hash=bar.content_hash,
        ),
        kind=FactKind.OBSERVED,
        category="price_bar",
        instrument=bar.instrument,
        event_time=bar.occurred_at,
        published_at=bar.available_at,
        available_at=bar.available_at,
        ingested_at=bar.available_at,
        valid_until=bar.available_at + dt.timedelta(seconds=max_age_seconds),
        source_id=bar.source_id,
        content_hash=bar.content_hash,
        attributes=price_attributes(
            open_=dec_str(bar.open),
            high=dec_str(bar.high),
            low=dec_str(bar.low),
            close=dec_str(bar.close),
            volume=bar.volume,
        ),
    )


def fact_from_quote(quote: MarketQuote, *, max_age_seconds: int) -> Fact:
    """An ``OBSERVED`` top-of-book fact. The mid is the primary value."""
    if max_age_seconds < 1:
        msg = "max_age_seconds must be positive"
        raise ValueError(msg)
    return Fact(
        fact_id=fact_id_for(
            category="quote",
            instrument=quote.instrument,
            source_id=quote.source_id,
            event_time=quote.occurred_at,
            content_hash=quote.content_hash,
        ),
        kind=FactKind.OBSERVED,
        category="quote",
        instrument=quote.instrument,
        event_time=quote.occurred_at,
        published_at=quote.available_at,
        available_at=quote.available_at,
        ingested_at=quote.available_at,
        valid_until=quote.available_at + dt.timedelta(seconds=max_age_seconds),
        source_id=quote.source_id,
        content_hash=quote.content_hash,
        attributes={
            "bid": dec_str(quote.bid),
            "ask": dec_str(quote.ask),
            "value": dec_str(quote.mid()),
        },
    )
