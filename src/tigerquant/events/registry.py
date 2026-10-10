"""Closed event registry.

An unknown ``event_type`` is a hard error. Silently skipping an unrecognised record
would let a partially-upgraded deployment replay a journal while ignoring half of it,
which is exactly the kind of quiet divergence ADR-0004 exists to prevent.
"""

from __future__ import annotations

import operator
from functools import reduce
from typing import Annotated, Any

from pydantic import Field, TypeAdapter

from tigerquant.core.errors import CanonicalizationError
from tigerquant.events.base import EventBase
from tigerquant.events.decision import DecisionProposed, DecisionRejected, SnapshotCreated
from tigerquant.events.execution import (
    OrderAcknowledged,
    OrderCanceled,
    OrderCancelRequested,
    OrderFilled,
    OrderIntentCreated,
    OrderPartiallyFilled,
    OrderQuarantined,
    OrderRejected,
    OrderSubmissionUnknown,
    OrderSubmitted,
)
from tigerquant.events.market import MarketBar, MarketQuote
from tigerquant.events.portfolio import LedgerMarked, PositionChanged
from tigerquant.events.risk import AlertRaised, RiskEvaluated, RiskStateChanged

__all__ = ["EVENT_MODELS", "EVENT_TYPES", "EventAdapter", "event_type_names", "parse_event"]

EVENT_MODELS: tuple[type[EventBase], ...] = (
    MarketBar,
    MarketQuote,
    SnapshotCreated,
    DecisionProposed,
    DecisionRejected,
    RiskEvaluated,
    RiskStateChanged,
    AlertRaised,
    OrderIntentCreated,
    OrderSubmitted,
    OrderAcknowledged,
    OrderSubmissionUnknown,
    OrderRejected,
    OrderPartiallyFilled,
    OrderFilled,
    OrderCancelRequested,
    OrderCanceled,
    OrderQuarantined,
    PositionChanged,
    LedgerMarked,
)

#: type name -> model
EVENT_TYPES: dict[str, type[EventBase]] = {
    str(model.model_fields["event_type"].default): model for model in EVENT_MODELS
}

_UnionType: Any = reduce(operator.or_, EVENT_MODELS)
EventAdapter: TypeAdapter[EventBase] = TypeAdapter(
    Annotated[_UnionType, Field(discriminator="event_type")]
)


def event_type_names() -> tuple[str, ...]:
    """Registered event type names, sorted for stable output."""
    return tuple(sorted(EVENT_TYPES))


def parse_event(data: dict[str, Any]) -> EventBase:
    """Parse a mapping into the concrete event model.

    Raises
    ------
    CanonicalizationError
        If ``event_type`` is missing or not registered.
    """
    event_type = data.get("event_type")
    if not isinstance(event_type, str):
        msg = f"journal record has no usable event_type: {event_type!r}"
        raise CanonicalizationError(msg)
    if event_type not in EVENT_TYPES:
        msg = f"unknown event_type {event_type!r}; known types: {', '.join(event_type_names())}"
        raise CanonicalizationError(msg)
    return EventAdapter.validate_python(data)
