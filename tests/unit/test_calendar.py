"""US equity trading calendar tests.

Holidays and early closes are checked against dates published by the exchange, so a
regression in the rules is caught rather than silently shifting the approved order window.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tigerquant.core.errors import ConfigurationError
from tigerquant.market.calendar import (
    EARLIEST_SUPPORTED_YEAR,
    LATEST_SUPPORTED_YEAR,
    UsEquityCalendar,
    holidays_for_year,
)


def at_utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)


@pytest.fixture
def calendar() -> UsEquityCalendar:
    return UsEquityCalendar()


class TestHolidays:
    @pytest.mark.parametrize(
        ("year", "month", "day", "name"),
        [
            (2026, 1, 1, "New Year's Day"),
            (2026, 1, 19, "Martin Luther King Jr. Day"),
            (2026, 2, 16, "Washington's Birthday"),
            (2026, 4, 3, "Good Friday"),
            (2026, 5, 25, "Memorial Day"),
            (2026, 6, 19, "Juneteenth National Independence Day"),
            (2026, 7, 3, "Independence Day"),  # 4 July 2026 is a Saturday
            (2026, 9, 7, "Labor Day"),
            (2026, 11, 26, "Thanksgiving Day"),
            (2026, 12, 25, "Christmas Day"),
        ],
    )
    def test_published_2026_holidays(
        self, calendar: UsEquityCalendar, year: int, month: int, day: int, name: str
    ) -> None:
        assert calendar.holiday_name(dt.date(year, month, day)) == name
        assert not calendar.is_trading_day(dt.date(year, month, day))

    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            (2024, dt.date(2024, 3, 29)),  # Good Friday 2024
            (2025, dt.date(2025, 4, 18)),
            (2027, dt.date(2027, 3, 26)),
            (2030, dt.date(2030, 4, 19)),
        ],
    )
    def test_good_friday_matches_published_dates(
        self, calendar: UsEquityCalendar, year: int, expected: dt.date
    ) -> None:
        assert calendar.holiday_name(expected) == "Good Friday"

    def test_saturday_holiday_is_observed_on_friday(self, calendar: UsEquityCalendar) -> None:
        # 4 July 2026 is a Saturday, so the market closes on Friday 3 July.
        assert calendar.holiday_name(dt.date(2026, 7, 3)) == "Independence Day"
        assert not calendar.is_trading_day(dt.date(2026, 7, 3))

    def test_sunday_holiday_is_observed_on_monday(self, calendar: UsEquityCalendar) -> None:
        # 25 December 2022 was a Sunday, observed Monday 26 December.
        assert calendar.holiday_name(dt.date(2022, 12, 26)) == "Christmas Day"

    def test_holiday_set_is_a_full_year(self, calendar: UsEquityCalendar) -> None:
        assert len(holidays_for_year(2026)) == 10

    def test_out_of_window_year_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="covers"):
            holidays_for_year(EARLIEST_SUPPORTED_YEAR - 1)
        with pytest.raises(ConfigurationError, match="covers"):
            holidays_for_year(LATEST_SUPPORTED_YEAR + 1)


class TestEarlyCloses:
    def test_day_after_thanksgiving(self, calendar: UsEquityCalendar) -> None:
        bounds = calendar.session_bounds(dt.date(2026, 11, 27))
        assert bounds is not None
        assert bounds.is_early_close
        assert bounds.closes == dt.time(13, 0)
        assert bounds.approved_end == dt.time(12, 55)

    def test_christmas_eve_when_a_weekday(self, calendar: UsEquityCalendar) -> None:
        bounds = calendar.session_bounds(dt.date(2026, 12, 24))
        assert bounds is not None
        assert bounds.is_early_close

    def test_christmas_eve_on_a_weekend_is_not_a_session(self, calendar: UsEquityCalendar) -> None:
        # 24 December 2022 was a Saturday.
        assert calendar.session_bounds(dt.date(2022, 12, 24)) is None

    def test_july_third_when_july_fourth_is_a_weekday(self, calendar: UsEquityCalendar) -> None:
        bounds = calendar.session_bounds(dt.date(2025, 7, 3))
        assert bounds is not None
        assert bounds.is_early_close

    def test_july_third_when_july_fourth_is_a_saturday_is_closed(
        self, calendar: UsEquityCalendar
    ) -> None:
        # 2026: the observed holiday is Friday 3 July, so there is no session at all.
        assert calendar.session_bounds(dt.date(2026, 7, 3)) is None

    def test_regular_day_is_not_early(self, calendar: UsEquityCalendar) -> None:
        bounds = calendar.session_bounds(dt.date(2026, 3, 2))
        assert bounds is not None
        assert bounds.is_regular()
        assert bounds.closes == dt.time(16, 0)


class TestSessionWindows:
    def test_open_on_a_regular_weekday(self, calendar: UsEquityCalendar) -> None:
        # 2026-03-02 is a Monday; 14:40Z == 09:40 EST.
        assert calendar.is_open(at_utc(2026, 3, 2, 14, 40))
        assert calendar.is_within_approved_window(at_utc(2026, 3, 2, 14, 40))

    def test_opening_auction_is_outside_the_approved_window(
        self, calendar: UsEquityCalendar
    ) -> None:
        assert calendar.is_open(at_utc(2026, 3, 2, 14, 30))
        assert not calendar.is_within_approved_window(at_utc(2026, 3, 2, 14, 30))

    def test_late_afternoon_is_still_tradeable(self, calendar: UsEquityCalendar) -> None:
        # 20:30Z == 15:30 EST: inside the session and inside the approved window.
        assert calendar.is_open(at_utc(2026, 3, 2, 20, 30))
        assert calendar.is_within_approved_window(at_utc(2026, 3, 2, 20, 30))

    def test_closing_auction_is_outside_the_approved_window(
        self, calendar: UsEquityCalendar
    ) -> None:
        # 20:57Z == 15:57 EST: the session is still open, the approved window is not.
        assert calendar.is_open(at_utc(2026, 3, 2, 20, 57))
        assert not calendar.is_within_approved_window(at_utc(2026, 3, 2, 20, 57))

    def test_after_the_close_is_closed(self, calendar: UsEquityCalendar) -> None:
        assert not calendar.is_open(at_utc(2026, 3, 2, 21, 0))

    def test_holiday_is_closed_all_day(self, calendar: UsEquityCalendar) -> None:
        for hour in range(13, 22):
            assert not calendar.is_open(at_utc(2026, 12, 25, hour))

    def test_weekend_is_closed(self, calendar: UsEquityCalendar) -> None:
        assert not calendar.is_open(at_utc(2026, 3, 7, 15, 0))

    def test_dst_is_handled(self, calendar: UsEquityCalendar) -> None:
        """The same UTC hour is inside the session in winter and outside it in summer."""
        winter = at_utc(2026, 1, 5, 14, 40)  # 09:40 EST
        summer = at_utc(2026, 7, 6, 14, 40)  # 10:40 EDT
        assert calendar.is_within_approved_window(winter)
        assert calendar.is_within_approved_window(summer)
        # 13:35Z is 08:35 EST (pre-open) but 09:35 EDT (approved window).
        assert not calendar.is_open(at_utc(2026, 1, 5, 13, 35))
        assert calendar.is_within_approved_window(at_utc(2026, 7, 6, 13, 35))

    def test_early_close_shuts_the_window_early(self, calendar: UsEquityCalendar) -> None:
        # 2026-11-27 closes at 13:00 EST == 18:00Z; 13:30 local is past the close.
        assert calendar.is_open(at_utc(2026, 11, 27, 17, 0))
        assert not calendar.is_open(at_utc(2026, 11, 27, 19, 0))

    def test_non_datetime_is_rejected(self, calendar: UsEquityCalendar) -> None:
        with pytest.raises(TypeError, match="must be a datetime"):
            calendar.is_open("2026-03-02")

    def test_halted_instruments(self) -> None:
        halted = UsEquityCalendar(halted_instruments=("SPY",))
        assert halted.is_halted("SPY", at_utc(2026, 3, 2, 14, 40))
        assert not halted.is_halted("QQQ", at_utc(2026, 3, 2, 14, 40))

    def test_ad_hoc_closure(self) -> None:
        closed = UsEquityCalendar(ad_hoc_closures=(dt.date(2026, 3, 3),))
        assert not closed.is_trading_day(dt.date(2026, 3, 3))
        assert closed.holiday_name(dt.date(2026, 3, 3)) == "ad-hoc closure"

    def test_window_must_be_inside_the_session(self) -> None:
        with pytest.raises(Exception, match="inside the regular session"):
            UsEquityCalendar(approved_window_start=dt.time(8, 0))


class TestNavigation:
    def test_next_trading_day_skips_a_weekend(self, calendar: UsEquityCalendar) -> None:
        assert calendar.next_trading_day(dt.date(2026, 3, 6)) == dt.date(2026, 3, 9)

    def test_next_trading_day_skips_a_holiday(self, calendar: UsEquityCalendar) -> None:
        assert calendar.next_trading_day(dt.date(2026, 12, 24)) == dt.date(2026, 12, 28)

    def test_trading_days_in_a_range(self, calendar: UsEquityCalendar) -> None:
        days = calendar.trading_days(dt.date(2026, 3, 2), dt.date(2026, 3, 8))
        assert days == (
            dt.date(2026, 3, 2),
            dt.date(2026, 3, 3),
            dt.date(2026, 3, 4),
            dt.date(2026, 3, 5),
            dt.date(2026, 3, 6),
        )

    def test_most_years_have_about_252_trading_days(self, calendar: UsEquityCalendar) -> None:
        for year in (2024, 2025, 2026):
            count = len(calendar.trading_days(dt.date(year, 1, 1), dt.date(year, 12, 31)))
            assert 248 <= count <= 253, (year, count)
