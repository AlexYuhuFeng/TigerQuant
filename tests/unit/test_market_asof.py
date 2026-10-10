"""Market layer: fixture validation, deterministic feed, and the no-lookahead guard."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Mode
from tigerquant.core.errors import ConfigurationError, LookaheadError, MissingMarketData
from tigerquant.core.ids import IdFactory
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.journal import MemoryJournal
from tigerquant.events.market import MarketBar
from tigerquant.market.asof import PointInTimeView
from tigerquant.market.feed import DeterministicMarketFeed, interval_delta
from tigerquant.market.fixtures import ScenarioFixture, load_scenario
from tigerquant.market.session import TradingSession

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


def _bar(emitter: EventEmitter, close: str, minutes: int) -> MarketBar:
    occurred = AT + dt.timedelta(minutes=minutes)
    return emitter.emit(
        MarketBar,
        occurred_at=occurred,
        instrument="SPY",
        interval="1m",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        available_at=occurred + dt.timedelta(minutes=1),
        source_id="fixture:test",
        content_hash="sha256:" + "b" * 64,
        reference_price=close,
    )


class TestFixtures:
    def test_loads_and_validates(self, scenario_path: Path) -> None:
        scenario = load_scenario(scenario_path)
        assert scenario.scenario_id == "trend_up"
        assert scenario.mode is Mode.BACKTEST
        assert scenario.universe == ("SPY",)

    def test_missing_file_is_a_configuration_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            load_scenario(tmp_path / "nope.yaml")

    def test_duplicate_universe_entry_is_rejected(self, scenario_path: Path) -> None:
        scenario = load_scenario(scenario_path)
        with pytest.raises(Exception, match="duplicates"):
            ScenarioFixture.model_validate(
                {**scenario.model_dump(mode="json"), "universe": ["SPY", "SPY"]}
            )

    def test_universe_without_bars_is_rejected(self, scenario_path: Path) -> None:
        scenario = load_scenario(scenario_path)
        with pytest.raises(Exception, match="no bars"):
            ScenarioFixture.model_validate(
                {**scenario.model_dump(mode="json"), "universe": ["QQQ"]}
            )

    def test_live_mode_fixture_is_rejected(self, scenario_path: Path) -> None:
        scenario = load_scenario(scenario_path)
        with pytest.raises(Exception, match="LIVE"):
            ScenarioFixture.model_validate({**scenario.model_dump(mode="json"), "mode": "LIVE"})

    def test_inconsistent_ohlc_is_rejected(self, scenario_path: Path) -> None:
        scenario = load_scenario(scenario_path)
        payload = scenario.model_dump(mode="json")
        payload["instruments"]["SPY"]["bars"][0]["h"] = "1.00"
        with pytest.raises(Exception, match="high"):
            ScenarioFixture.model_validate(payload)

    def test_content_digest_is_stable(self, scenario: ScenarioFixture) -> None:
        assert scenario.content_digest() == scenario.content_digest()

    def test_interval_delta(self) -> None:
        from tigerquant.core.enums import Interval

        assert interval_delta(Interval.MIN_1) == dt.timedelta(minutes=1)
        assert interval_delta(Interval.DAY_1) == dt.timedelta(days=1)


class TestFeed:
    def test_schedule_is_a_total_order(self, scenario: ScenarioFixture) -> None:
        feed = DeterministicMarketFeed(scenario)
        schedule = feed.schedule()
        assert [step.index for step in schedule] == list(range(len(schedule)))
        keys = [(step.bar.t, step.symbol) for step in schedule]
        assert keys == sorted(keys)

    def test_drafts_are_deterministic(self, scenario: ScenarioFixture) -> None:
        feed = DeterministicMarketFeed(scenario)
        assert feed.drafts() == feed.drafts()

    def test_available_at_is_the_bar_close_not_the_open(self, scenario: ScenarioFixture) -> None:
        feed = DeterministicMarketFeed(scenario)
        draft = feed.drafts()[0]
        assert draft.available_at == draft.occurred_at + dt.timedelta(minutes=1)


class TestPointInTimeView:
    def _view(self) -> tuple[PointInTimeView, list[MarketBar]]:
        clock = SimulatedClock(AT)
        journal = MemoryJournal(Mode.BACKTEST)
        emitter = EventEmitter(
            mode=Mode.BACKTEST,
            sink=journal,
            ids=IdFactory("bt", clock),
            clock=clock,
            correlation_id="test",
        )
        bars = [_bar(emitter, close=str(600 + index), minutes=index) for index in range(3)]
        view = PointInTimeView.empty(AT).with_bars(bars, AT)
        return view, bars

    def test_bar_is_invisible_until_it_closes(self) -> None:
        view, bars = self._view()
        # A bar opening at 14:40 becomes available at 14:41.
        assert view.visible_bars("SPY") == ()
        assert view.future_bars("SPY") == tuple(bars)

    def test_bar_becomes_visible_exactly_at_available_at(self) -> None:
        _, bars = self._view()
        view = PointInTimeView.empty(AT).with_bars(bars, bars[0].available_at)
        assert view.visible_bars("SPY") == (bars[0],)

    def test_require_history_raises_when_too_few_bars(self) -> None:
        view, bars = self._view()
        view = PointInTimeView.empty(AT).with_bars(bars, bars[0].available_at)
        with pytest.raises(MissingMarketData, match="insufficient history"):
            view.require_history("SPY", 2)

    def test_missing_data_fails_closed(self) -> None:
        view, _ = self._view()
        with pytest.raises(MissingMarketData, match="fails closed"):
            view.latest_bar("SPY")

    def test_assert_available_rejects_a_future_bar(self) -> None:
        view, bars = self._view()
        with pytest.raises(LookaheadError, match="lookahead"):
            view.assert_available(bars[-1])

    def test_closes_only_sees_visible_bars(self) -> None:
        _, bars = self._view()
        view = PointInTimeView.empty(AT).with_bars(bars, bars[1].available_at)
        assert view.closes("SPY", 2) == (Decimal(600), Decimal(601))


class TestTradingSession:
    def test_open_during_regular_session(self) -> None:
        session = TradingSession()
        # 2026-03-02 is a Monday; 14:40Z is 09:40 in New York (EST, UTC-5).
        assert session.is_open(AT)
        assert session.is_within_approved_window(AT)

    def test_closed_before_the_open(self) -> None:
        session = TradingSession()
        assert not session.is_open(dt.datetime(2026, 3, 2, 13, 0, tzinfo=dt.UTC))

    def test_closed_at_the_weekend(self) -> None:
        session = TradingSession()
        assert not session.is_open(dt.datetime(2026, 3, 7, 15, 0, tzinfo=dt.UTC))

    def test_opening_auction_is_outside_the_approved_window(self) -> None:
        session = TradingSession()
        # 14:30Z == 09:30 New York: the session is open but orders are not approved.
        assert session.is_open(dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.UTC))
        assert not session.is_within_approved_window(dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.UTC))

    def test_halt_list(self) -> None:
        session = TradingSession(halted_instruments=("SPY",))
        assert session.is_halted("SPY", AT)
        assert not session.is_halted("QQQ", AT)

    def test_unknown_timezone_is_a_configuration_error(self) -> None:
        session = TradingSession(timezone="Mars/Olympus")
        with pytest.raises(ConfigurationError, match="timezone database"):
            session.is_open(AT)
