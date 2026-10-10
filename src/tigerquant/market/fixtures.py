"""Scenario fixtures.

A fixture is a complete, self-contained description of a synthetic market session.
It is data, never code: loading a fixture can only produce the bars and parameters it
declares, so a deterministic replay cannot fabricate market history (ADR-0011).

Bars are declared with a minute offset ``t`` relative to ``scenario.start`` so that
fixtures stay readable and stable regardless of calendar date.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tigerquant.core.canonical import digest_of
from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import Interval, Mode
from tigerquant.core.errors import ConfigurationError

__all__ = [
    "BarSpec",
    "BrokerFixture",
    "FillPolicy",
    "InstrumentFixture",
    "RiskFixture",
    "ScenarioFixture",
    "StrategyFixture",
    "load_scenario",
]

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]

FillPolicy = Literal["immediate", "next_bar", "partial", "reject", "timeout"]

#: US-listed equity/ETF ticker shape accepted in fixtures.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BarSpec(_Model):
    """One synthetic bar. ``t`` is minutes after the scenario start."""

    t: Annotated[int, Field(ge=0)]
    o: PositiveDecimal
    h: PositiveDecimal
    l: PositiveDecimal  # noqa: E741 - OHLC naming is standard
    c: PositiveDecimal
    v: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _check_ohlc(self) -> BarSpec:
        high = max(self.o, self.c, self.l)
        low = min(self.o, self.c, self.h)
        if self.h < high:
            msg = f"bar at t={self.t}: high {self.h} is below open/close/low"
            raise ValueError(msg)
        if self.l > low:
            msg = f"bar at t={self.t}: low {self.l} is above open/close/high"
            raise ValueError(msg)
        return self

    def timestamp(self, start: dt.datetime) -> dt.datetime:
        """Absolute bar-open time."""
        return ensure_utc(start) + dt.timedelta(minutes=self.t)


class InstrumentFixture(_Model):
    """Bars for one instrument. The symbol is the key in ``ScenarioFixture.instruments``."""

    bars: Annotated[tuple[BarSpec, ...], Field(min_length=2)]


class StrategyFixture(_Model):
    """Parameters of the deterministic reference strategy (labelled SIMULATED)."""

    kind: Literal["momentum"] = "momentum"
    lookback: Annotated[int, Field(ge=1, le=500)] = 3
    entry_threshold_bps: NonNegativeDecimal = Decimal(10)
    exit_threshold_bps: NonNegativeDecimal = Decimal(10)
    target_weight: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.05")
    horizon: str = "5 trading days"


class RiskFixture(_Model):
    """Baseline policy values. CR-07 extends this set (see docs/CR-01-VERTICAL-SLICE.md §2)."""

    gross_exposure_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.5")
    max_symbol_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.1")
    max_order_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.02")
    max_mark_age_seconds: Annotated[int, Field(ge=1)] = 900
    allow_margin: bool = False
    allow_short: bool = False
    allow_options: bool = False
    max_orders_per_cycle: Annotated[int, Field(ge=0)] = 1
    halted: bool = False


class BrokerFixture(_Model):
    """Deterministic fake-broker behaviour for this scenario."""

    fill_policy: FillPolicy = "immediate"
    commission_per_share: NonNegativeDecimal = Decimal("0.005")
    slippage_bps: NonNegativeDecimal = Decimal(0)
    #: Fraction of the requested quantity filled by the first partial fill.
    partial_fill_fraction: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.5")
    #: Simulate the request being accepted then the response being lost (ADR-0008 §3).
    lose_acknowledgement: bool = False
    #: Simulated round-trip latency, in milliseconds of scenario time.
    latency_ms: Annotated[int, Field(ge=0)] = 0


class ScenarioFixture(_Model):
    """A complete synthetic session."""

    scenario_id: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9_\-]+$")]
    description: str = ""
    mode: Mode = Mode.BACKTEST
    start: dt.datetime
    interval: Interval = Interval.MIN_1
    initial_cash: PositiveDecimal = Decimal("100000.00")
    universe: Annotated[tuple[str, ...], Field(min_length=1)]
    max_fact_age_seconds: Annotated[int, Field(ge=1)] = 900
    strategy: StrategyFixture = StrategyFixture()
    risk: RiskFixture = RiskFixture()
    broker: BrokerFixture = BrokerFixture()
    instruments: Annotated[dict[str, InstrumentFixture], Field(min_length=1)]

    @field_validator("mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> object:
        """Accept ``backtest`` as well as ``BACKTEST`` in fixture files."""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> ScenarioFixture:
        if not self.start.tzinfo:
            msg = "scenario.start must be timezone-aware (UTC preferred)"
            raise ValueError(msg)
        missing = [sym for sym in self.universe if sym not in self.instruments]
        if missing:
            msg = f"universe references instruments with no bars: {missing}"
            raise ValueError(msg)
        for symbol in self.instruments:
            if not _SYMBOL_RE.match(symbol):
                msg = f"instrument key {symbol!r} is not a valid US equity symbol"
                raise ValueError(msg)
        duplicates = len(self.universe) != len(set(self.universe))
        if duplicates:
            msg = "universe must not contain duplicates (ordering must be total)"
            raise ValueError(msg)
        for symbol, instrument in self.instruments.items():
            offsets = [bar.t for bar in instrument.bars]
            if offsets != sorted(offsets):
                msg = f"{symbol}: bars must be ordered by ascending t"
                raise ValueError(msg)
            if len(set(offsets)) != len(offsets):
                msg = f"{symbol}: duplicate bar offsets"
                raise ValueError(msg)
        if self.mode is Mode.LIVE:
            msg = "fixtures may not declare LIVE mode; live runs never use synthetic data"
            raise ValueError(msg)
        return self

    def content_digest(self) -> str:
        """Digest of the fully-resolved fixture, recorded in the audit journal."""
        return digest_of(self)


def load_scenario(path: Path | str) -> ScenarioFixture:
    """Load and strictly validate a scenario fixture from YAML."""
    fixture_path = Path(path)
    if not fixture_path.is_file():
        msg = f"scenario fixture not found: {fixture_path}"
        raise ConfigurationError(msg)
    try:
        raw = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"scenario fixture {fixture_path} is not valid YAML: {exc}"
        raise ConfigurationError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"scenario fixture {fixture_path} must contain a mapping at the top level"
        raise ConfigurationError(msg)
    try:
        return ScenarioFixture.model_validate(raw)
    except Exception as exc:
        msg = f"scenario fixture {fixture_path} failed validation: {exc}"
        raise ConfigurationError(msg) from exc
