"""Operational supervision: the watchdog, drift monitoring and the supervisor hooks.

Two independent supervisors live here, and they are deliberately separate:

* :class:`~tigerquant.ops.watchdog.Watchdog` decides whether new exposure may be opened. It
  is the only thing that can halt.
* :class:`~tigerquant.ops.drift_monitor.DriftMonitor` decides whether the journal and the
  database still agree, and *reports* that fact. It never halts anything and never repairs
  anything — giving a second component the power to halt would create a second place for a
  halt to be wrong, and repairing a drift would destroy the evidence needed to explain it.
"""

from __future__ import annotations

from tigerquant.ops.drift_monitor import (
    DEFAULT_DRIFT_POLICY,
    DriftAlertPolicy,
    DriftCheckResult,
    DriftMonitor,
    drift_ratio,
    interval_runner,
)
from tigerquant.ops.watchdog import (
    DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS,
    Watchdog,
    WatchdogVerdict,
)

__all__ = [
    "DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS",
    "DEFAULT_DRIFT_POLICY",
    "DriftAlertPolicy",
    "DriftCheckResult",
    "DriftMonitor",
    "Watchdog",
    "WatchdogVerdict",
    "drift_ratio",
    "interval_runner",
]
