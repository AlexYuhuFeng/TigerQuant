"""Operational supervision: the watchdog, alerts and supervisor hooks (ADR-0007 §6)."""

from __future__ import annotations

from tigerquant.ops.watchdog import (
    DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS,
    Watchdog,
    WatchdogVerdict,
)

__all__ = [
    "DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS",
    "Watchdog",
    "WatchdogVerdict",
]
