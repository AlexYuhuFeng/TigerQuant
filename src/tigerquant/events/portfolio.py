"""Portfolio-plane events. The ledger is a projection over these (ADR-0004 §6)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from tigerquant.events.base import AwareUTC, EventBase, NonEmptyStr

__all__ = ["LedgerMarked", "PositionChanged"]


class PositionChanged(EventBase):
    """Position after applying one fill. Quantities are integral shares."""

    event_type: Literal["PositionChanged"] = "PositionChanged"

    instrument: NonEmptyStr
    quantity: int
    average_cost: Decimal = Field(ge=0)
    realized_pnl: Decimal
    mark_price: Decimal = Field(ge=0)
    unrealized_pnl: Decimal


class LedgerMarked(EventBase):
    """End-of-cycle ledger valuation, used as the basis for the next risk evaluation."""

    event_type: Literal["LedgerMarked"] = "LedgerMarked"

    as_of: AwareUTC
    cash: Decimal
    equity: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    #: Digest of (positions, cash, marks) so a risk outcome can be tied to it.
    ledger_digest: NonEmptyStr
