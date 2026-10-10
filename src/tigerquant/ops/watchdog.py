"""Independent watchdog: the component that stops the system.

The watchdog exists because *the thing that decides to trade must not be the thing that
decides whether trading is still allowed*. It is a separate, deterministic evaluator that
watches accumulated state and conditions, and its only power is to **stop** — engaging the
halt or freeze. It cannot trade, cannot approve anything, and cannot clear a halt: clearing
requires the owner (ADR-0007 §6).

It is also independent of the model. A provider outage, a hung decision, or a model that
argues persuasively for one more trade changes nothing here, because the watchdog reads
metrics and conditions, never prose (acceptance gate 11).

Each condition maps to a ``ReasonCode`` so the reason for a halt is machine-readable in the
journal, and each verdict carries the measured value and the limit so an operator can see
*how far* past the line the system went rather than only that it crossed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import ReasonCode, Severity
from tigerquant.risk.state import Actor, RiskState

if TYPE_CHECKING:
    from tigerquant.risk.metrics import RiskMetrics
    from tigerquant.risk.policy import RiskPolicy

__all__ = ["DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS", "Watchdog", "WatchdogVerdict"]

#: How far our clock may differ from an external source before we stop opening exposure.
#: Beyond this, staleness and window checks are evaluating the wrong instant.
DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class WatchdogVerdict:
    """One observed condition."""

    code: ReasonCode
    severity: Severity
    detail: str
    #: Whether this condition alone should stop new exposure immediately.
    halts: bool = True

    def describe(self) -> str:
        return f"[{self.severity.value}] {self.code.value}: {self.detail}"


class Watchdog:
    """Deterministic supervisor over accumulated risk state.

    Holds a reference to :class:`~tigerquant.risk.state.RiskState` so it can engage a halt,
    and nothing else. There is deliberately no reference to a broker, a model, or a gateway.
    """

    __slots__ = ("_actor", "_clock_tolerance", "_policy", "_state")

    def __init__(
        self,
        *,
        policy: RiskPolicy,
        state: RiskState,
        clock_skew_tolerance_seconds: int = DEFAULT_CLOCK_SKEW_TOLERANCE_SECONDS,
    ) -> None:
        self._policy = policy
        self._state = state
        self._actor = Actor.WATCHDOG
        self._clock_tolerance = clock_skew_tolerance_seconds

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def actor(self) -> Actor:
        """The watchdog's identity. It may halt; it may never clear."""
        return self._actor

    # -- inspection --------------------------------------------------------------

    def inspect(
        self,
        *,
        metrics: RiskMetrics | None,
        blocking_intents: int = 0,
        account_reconciled: bool = True,
        clock_skew_seconds: Decimal | None = None,
        broker_reachable: bool = True,
    ) -> tuple[WatchdogVerdict, ...]:
        """Evaluate every condition. Returns only the ones that fired.

        An empty tuple means nothing is wrong — it is not a statement that trading is safe,
        which remains the gateway's decision.
        """
        verdicts: list[WatchdogVerdict] = []

        if metrics is None:
            verdicts.append(
                WatchdogVerdict(
                    code=ReasonCode.RISK_HALTED,
                    severity=Severity.CRITICAL,
                    detail=(
                        "no session metrics are available, so no loss limit can be evaluated; "
                        "halting rather than trading blind"
                    ),
                )
            )
        else:
            loss = metrics.daily_loss_fraction()
            if loss > self._policy.max_daily_loss_fraction:
                verdicts.append(
                    WatchdogVerdict(
                        code=ReasonCode.DAILY_LOSS_EXCEEDED,
                        severity=Severity.CRITICAL,
                        detail=(
                            f"daily loss {loss:.6f} exceeds the limit "
                            f"{self._policy.max_daily_loss_fraction}"
                        ),
                    )
                )
            drawdown = metrics.drawdown_fraction()
            if drawdown > self._policy.max_drawdown_fraction:
                verdicts.append(
                    WatchdogVerdict(
                        code=ReasonCode.DRAWDOWN_EXCEEDED,
                        severity=Severity.CRITICAL,
                        detail=(
                            f"drawdown {drawdown:.6f} exceeds the limit "
                            f"{self._policy.max_drawdown_fraction}"
                        ),
                    )
                )
            turnover = metrics.turnover_fraction()
            if turnover > self._policy.max_turnover_fraction:
                verdicts.append(
                    WatchdogVerdict(
                        code=ReasonCode.TURNOVER_EXCEEDED,
                        severity=Severity.WARNING,
                        detail=(
                            f"turnover {turnover:.6f} exceeds the limit "
                            f"{self._policy.max_turnover_fraction}"
                        ),
                    )
                )

        if blocking_intents > 0:
            verdicts.append(
                WatchdogVerdict(
                    code=ReasonCode.UNRECONCILED,
                    severity=Severity.CRITICAL,
                    detail=(
                        f"{blocking_intents} unresolved order intent(s); new exposure must "
                        "stay blocked until reconciliation completes"
                    ),
                )
            )

        if not account_reconciled:
            verdicts.append(
                WatchdogVerdict(
                    code=ReasonCode.UNRECONCILED,
                    severity=Severity.CRITICAL,
                    detail=(
                        "broker state does not reconcile with the ledger; neither record can "
                        "be trusted until a human resolves the difference"
                    ),
                )
            )

        if not broker_reachable:
            verdicts.append(
                WatchdogVerdict(
                    code=ReasonCode.UNRECONCILED,
                    severity=Severity.CRITICAL,
                    detail=(
                        "the broker is unreachable, so order state cannot be reconciled; "
                        "refusing new exposure"
                    ),
                )
            )

        if clock_skew_seconds is not None and abs(clock_skew_seconds) > Decimal(
            self._clock_tolerance
        ):
            verdicts.append(
                WatchdogVerdict(
                    code=ReasonCode.CLOCK_SKEW,
                    severity=Severity.CRITICAL,
                    detail=(
                        f"clock skew {clock_skew_seconds}s exceeds the "
                        f"{self._clock_tolerance}s tolerance; staleness and market-window "
                        "checks would be evaluating the wrong instant"
                    ),
                )
            )

        return tuple(verdicts)

    # -- action ------------------------------------------------------------------

    def engage(self, verdicts: tuple[WatchdogVerdict, ...], *, at: dt.datetime) -> bool:
        """Halt if any verdict calls for it. Returns whether the state changed.

        Every halting verdict is folded into a single ``halt`` call: ``RiskState`` records
        the first reason and ignores repeats, so a storm of conditions produces one halt
        and one history entry rather than a flood of alerts.
        """
        halting = [verdict for verdict in verdicts if verdict.halts]
        if not halting:
            return False
        first = halting[0]
        detail = "; ".join(verdict.detail for verdict in halting)
        return self._state.halt(
            actor=self._actor,
            at=ensure_utc(at),
            reason_code=first.code,
            detail=detail,
        )

    def observe(
        self,
        *,
        at: dt.datetime,
        metrics: RiskMetrics | None,
        blocking_intents: int = 0,
        account_reconciled: bool = True,
        clock_skew_seconds: Decimal | None = None,
        broker_reachable: bool = True,
    ) -> tuple[WatchdogVerdict, ...]:
        """Inspect and engage in one step. Returns the verdicts that fired."""
        verdicts = self.inspect(
            metrics=metrics,
            blocking_intents=blocking_intents,
            account_reconciled=account_reconciled,
            clock_skew_seconds=clock_skew_seconds,
            broker_reachable=broker_reachable,
        )
        self.engage(verdicts, at=at)
        return verdicts

    def summarise(self) -> str:
        if not self._state.blocks_new_exposure():
            return "watchdog: no halt engaged"
        reason = self._state.reason_code.value if self._state.reason_code else "unknown"
        return f"watchdog: new exposure halted ({reason})"
