"""Trading session windows.

CR-01 uses an explicit weekday/time window in a named IANA timezone. CR-05 replaces this
with a real exchange calendar (holidays, half-days, halts) behind the same
:class:`~tigerquant.risk.policy.TradingSessionView` interface, so the risk gateway does
not change.

The window is deliberately conservative: orders are only allowed between
``approved_window_start`` and ``approved_window_end``, which are inside the regular
session. Acceptance gate 14 ("market closed, halted or beyond the approved order window
→ reject unless a documented compatible policy applies") is implemented here.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tigerquant.core.clock import ensure_utc
from tigerquant.core.errors import ConfigurationError

__all__ = ["TradingSession"]

#: Regular US equity session, exchange local time.
_DEFAULT_OPEN = dt.time(9, 30)
_DEFAULT_CLOSE = dt.time(16, 0)


class TradingSession(BaseModel):
    """Weekday/time trading window with an approved order window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: Annotated[str, Field(min_length=1)] = "America/New_York"
    session_open: dt.time = _DEFAULT_OPEN
    session_close: dt.time = _DEFAULT_CLOSE
    #: Orders are refused outside this window even during the session (open/close auction
    #: and closing-cross protection). CR-01 default: the first and last 5 minutes.
    approved_window_start: dt.time = dt.time(9, 35)
    approved_window_end: dt.time = dt.time(15, 55)
    weekdays: Annotated[tuple[int, ...], Field(min_length=1)] = (1, 2, 3, 4, 5)
    halted_instruments: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_windows(self) -> TradingSession:
        if not self.session_open < self.session_close:
            msg = "session_open must precede session_close"
            raise ValueError(msg)
        if not self.session_open <= self.approved_window_start < self.approved_window_end:
            msg = "approved window must start inside the session and before it ends"
            raise ValueError(msg)
        if self.approved_window_end > self.session_close:
            msg = "approved window end must not be after session close"
            raise ValueError(msg)
        if any(day < 1 or day > 7 for day in self.weekdays):
            msg = "weekdays must be ISO weekday numbers 1..7"
            raise ValueError(msg)
        return self

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - environment problem
            msg = (
                f"timezone database does not know {self.timezone!r}; install the 'tzdata' "
                "package (Windows has no system IANA database)"
            )
            raise ConfigurationError(msg) from exc

    def local(self, instant: dt.datetime) -> dt.datetime:
        """Convert an instant to exchange-local time."""
        return ensure_utc(instant).astimezone(self._zone())

    def is_open(self, instant: object) -> bool:
        """True when the regular session is open at ``instant``."""
        if not isinstance(instant, dt.datetime):
            msg = f"instant must be a datetime, got {type(instant).__name__}"
            raise TypeError(msg)
        local = self.local(instant)
        if local.isoweekday() not in self.weekdays:
            return False
        return self.session_open <= local.time() < self.session_close

    def is_within_approved_window(self, instant: object) -> bool:
        """True when new orders are permitted at ``instant``."""
        if not isinstance(instant, dt.datetime):
            msg = f"instant must be a datetime, got {type(instant).__name__}"
            raise TypeError(msg)
        local = self.local(instant)
        if local.isoweekday() not in self.weekdays:
            return False
        return self.approved_window_start <= local.time() < self.approved_window_end

    def is_halted(self, instrument: str, instant: object) -> bool:
        """True when the instrument is halted. CR-01 uses a static halt list."""
        del instant  # halts are not yet time-scoped; CR-05 adds the real feed
        return instrument in self.halted_instruments
