"""Event schemas.

Every state change in TigerQuant is an event. Events are frozen pydantic models with a
common envelope (identity, mode, ordering, timestamps, causality) plus a
type-specific payload, and they are the only thing the audit journal stores (ADR-0004).

Schema version ``1.0.0`` is part of the on-disk contract.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import Mode

__all__ = [
    "ENVELOPE_FIELDS",
    "SCHEMA_VERSION",
    "AwareUTC",
    "EventBase",
    "NonEmptyStr",
    "NonNegativeInt",
    "PositiveDecimal",
    "PositiveInt",
]

SCHEMA_VERSION = "1.0.0"

#: Envelope field names, in canonical order. Everything else on an event is payload.
ENVELOPE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "event_id",
    "event_type",
    "mode",
    "seq",
    "occurred_at",
    "recorded_at",
    "correlation_id",
    "causation_id",
)


def _aware_utc(value: dt.datetime) -> dt.datetime:
    """Reject naive datetimes and normalise to UTC (ADR-0011 §6)."""
    return ensure_utc(value)


AwareUTC = Annotated[dt.datetime, AfterValidator(_aware_utc)]
NonEmptyStr = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]


class EventBase(BaseModel):
    """Common envelope for every event."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=False)

    #: Discriminator. Subclasses narrow this to a ``Literal``.
    event_type: str
    schema_version: str = SCHEMA_VERSION
    event_id: NonEmptyStr
    mode: Mode
    #: Strictly increasing within one journal; assigned by the event sequencer.
    seq: NonNegativeInt
    #: When the fact happened in the world (venue time).
    occurred_at: AwareUTC
    #: When we recorded it. Equal to ``occurred_at`` in replay.
    recorded_at: AwareUTC
    #: Groups all events caused by one pipeline cycle.
    correlation_id: NonEmptyStr
    #: The event that directly caused this one, if any.
    causation_id: str | None = None

    ENVELOPE_FIELDS: ClassVar[tuple[str, ...]] = ENVELOPE_FIELDS

    def envelope(self) -> dict[str, Any]:
        """Envelope fields as a plain dict, in canonical order."""
        return {name: getattr(self, name) for name in ENVELOPE_FIELDS}

    def payload(self) -> dict[str, Any]:
        """Everything that is not part of the envelope."""
        return {
            key: value
            for key, value in self.model_dump(mode="python").items()
            if key not in ENVELOPE_FIELDS
        }
