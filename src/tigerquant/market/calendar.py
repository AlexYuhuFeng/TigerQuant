"""US equity trading calendar.

Replaces the CR-01 weekday/time placeholder with real session rules (ADR-0002 §5,
acceptance gate 14): weekends, the ten NYSE holidays and their observed dates, early
closes, and Easter-based Good Friday. DST is handled by :mod:`zoneinfo` rather than by a
fixed UTC offset, because the regular session shifts by an hour twice a year.

The calendar is a *fact about the market*, so it lives in :mod:`tigerquant.market`
alongside the other market-structure code and implements the same
:class:`~tigerquant.risk.policy.TradingSessionView` protocol as the CR-01 placeholder, so
the risk gateway is unchanged.

Accuracy note: this implements the published NYSE/Nasdaq regular-session rules for
2020-2035 with the standard observation conventions. It is not a licensed exchange
calendar, ad-hoc closures (weather, national mourning) are **not** modelled, and it must
not be the only guard against trading a closed market — the live data source's own
session state remains authoritative in PAPER/LIVE.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tigerquant.core.clock import ensure_utc
from tigerquant.core.errors import ConfigurationError

__all__ = ["EARLIEST_SUPPORTED_YEAR", "LATEST_SUPPORTED_YEAR", "SessionBounds", "UsEquityCalendar"]

NEW_YORK = "America/New_York"

#: The rules below are the published NYSE rules; outside this window they are guesses.
EARLIEST_SUPPORTED_YEAR = 2020
LATEST_SUPPORTED_YEAR = 2035

#: Regular session and the narrower window in which new orders are approved. The
#: approved window excludes the opening and closing auctions by default.
REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)
DEFAULT_APPROVED_START = dt.time(9, 35)
DEFAULT_APPROVED_END = dt.time(15, 55)
EARLY_CLOSE_APPROVED_END = dt.time(12, 55)


@dataclass(frozen=True, slots=True)
class SessionBounds:
    """Resolved session times for one trading day, in exchange-local time."""

    day: dt.date
    opens: dt.time
    closes: dt.time
    approved_start: dt.time
    approved_end: dt.time
    is_early_close: bool

    def is_regular(self) -> bool:
        return not self.is_early_close


def _easter_sunday(year: int) -> dt.date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher). Good Friday needs it."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The ``n``-th ``weekday`` (Mon=0) of ``month``."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    """The last ``weekday`` (Mon=0) of ``month``."""
    if month == 12:
        last = dt.date(year, 12, 31)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - dt.timedelta(days=offset)


def _observed(day: dt.date) -> dt.date:
    """NYSE observation convention: Saturday -> Friday, Sunday -> Monday."""
    if day.weekday() == 5:  # Saturday
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:  # Sunday
        return day + dt.timedelta(days=1)
    return day


def holidays_for_year(year: int) -> dict[dt.date, str]:
    """Observed NYSE holidays for ``year``, keyed by the observed date."""
    in_window = EARLIEST_SUPPORTED_YEAR <= year <= LATEST_SUPPORTED_YEAR
    if not in_window:
        msg = (
            f"the built-in US equity calendar covers {EARLIEST_SUPPORTED_YEAR}-"
            f"{LATEST_SUPPORTED_YEAR}; {year} is outside that window and would be a guess"
        )
        raise ConfigurationError(msg)

    entries: list[tuple[dt.date, str]] = [
        (_observed(dt.date(year, 1, 1)), "New Year's Day"),
        (_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day"),
        (_nth_weekday(year, 2, 0, 3), "Washington's Birthday"),
        (_easter_sunday(year) - dt.timedelta(days=2), "Good Friday"),
        (_last_weekday(year, 5, 0), "Memorial Day"),
        (_observed(dt.date(year, 6, 19)), "Juneteenth National Independence Day"),
        (_observed(dt.date(year, 7, 4)), "Independence Day"),
        (_nth_weekday(year, 9, 0, 1), "Labor Day"),
        (_nth_weekday(year, 11, 3, 4), "Thanksgiving Day"),
        (_observed(dt.date(year, 12, 25)), "Christmas Day"),
    ]
    return {day: name for day, name in entries if day.year == year}


def _early_closes_for_year(year: int) -> dict[dt.date, str]:
    """NYSE early closes (13:00 exchange-local).

    The rule reduces to: the candidate is a weekday and is not itself observed as a
    holiday. July 3 qualifies only when 4 July falls Tuesday-Friday, which is exactly the
    case where 3 July is a weekday; when 4 July lands on a Saturday the observed holiday
    is Friday the 3rd and the market is shut, which the holiday check already catches.
    """
    candidates: list[tuple[dt.date, str]] = [
        (_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1), "day after Thanksgiving"),
        (dt.date(year, 12, 24), "Christmas Eve"),
        (dt.date(year, 7, 3), "day before Independence Day"),
    ]
    holidays = holidays_for_year(year)
    out: dict[dt.date, str] = {}
    for day, name in candidates:
        if day.year != year or day.weekday() >= 5 or day in holidays:
            continue
        out[day] = name
    return out


class UsEquityCalendar(BaseModel):
    """Regular-session calendar for US listed equities and ETFs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: Annotated[str, Field(min_length=1)] = NEW_YORK
    approved_window_start: dt.time = DEFAULT_APPROVED_START
    approved_window_end: dt.time = DEFAULT_APPROVED_END
    #: Instruments currently halted. A live halt feed is a CR-09 integration.
    halted_instruments: tuple[str, ...] = ()
    #: Ad-hoc closures (weather, national mourning) supplied by an operator.
    ad_hoc_closures: tuple[dt.date, ...] = ()

    @model_validator(mode="after")
    def _check_window(self) -> UsEquityCalendar:
        if not REGULAR_OPEN <= self.approved_window_start < self.approved_window_end:
            msg = "approved_window_start must be inside the regular session"
            raise ValueError(msg)
        if self.approved_window_end > REGULAR_CLOSE:
            msg = "approved_window_end must not be after the regular close"
            raise ValueError(msg)
        return self

    # -- time helpers ------------------------------------------------------------

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
        return ensure_utc(instant).astimezone(self._zone())

    # -- calendar facts ----------------------------------------------------------

    def holidays(self, year: int) -> dict[dt.date, str]:
        return holidays_for_year(year)

    def early_closes(self, year: int) -> dict[dt.date, str]:
        return _early_closes_for_year(year)

    def holiday_name(self, day: dt.date) -> str | None:
        if day in self.ad_hoc_closures:
            return "ad-hoc closure"
        return holidays_for_year(day.year).get(day)

    def is_trading_day(self, day: dt.date) -> bool:
        if day.weekday() >= 5:
            return False
        return self.holiday_name(day) is None

    def session_bounds(self, day: dt.date) -> SessionBounds | None:
        """Session times for ``day``, or ``None`` when the market is closed."""
        if not self.is_trading_day(day):
            return None
        early = day in _early_closes_for_year(day.year)
        closes = EARLY_CLOSE if early else REGULAR_CLOSE
        approved_end = (
            min(self.approved_window_end, EARLY_CLOSE_APPROVED_END)
            if early
            else self.approved_window_end
        )
        return SessionBounds(
            day=day,
            opens=REGULAR_OPEN,
            closes=closes,
            approved_start=self.approved_window_start,
            approved_end=approved_end,
            is_early_close=early,
        )

    def next_trading_day(self, day: dt.date) -> dt.date:
        candidate = day + dt.timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate += dt.timedelta(days=1)
        msg = f"no trading day found within 30 days of {day.isoformat()}"
        raise ConfigurationError(msg)

    def trading_days(self, start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
        days: list[dt.date] = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += dt.timedelta(days=1)
        return tuple(days)

    # -- TradingSessionView protocol --------------------------------------------

    def is_open(self, instant: object) -> bool:
        """True during the regular session (including an early close)."""
        if not isinstance(instant, dt.datetime):
            msg = f"instant must be a datetime, got {type(instant).__name__}"
            raise TypeError(msg)
        local = self.local(instant)
        bounds = self.session_bounds(local.date())
        if bounds is None:
            return False
        return bounds.opens <= local.time() < bounds.closes

    def is_within_approved_window(self, instant: object) -> bool:
        """True when new orders are permitted at ``instant``."""
        if not isinstance(instant, dt.datetime):
            msg = f"instant must be a datetime, got {type(instant).__name__}"
            raise TypeError(msg)
        local = self.local(instant)
        bounds = self.session_bounds(local.date())
        if bounds is None:
            return False
        return bounds.approved_start <= local.time() < bounds.approved_end

    def is_halted(self, instrument: str, instant: object) -> bool:
        del instant  # halts are not yet time-scoped; the live halt feed is CR-09 work
        return instrument in self.halted_instruments
