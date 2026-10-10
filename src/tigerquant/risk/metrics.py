"""Risk metrics: the running quantities the loss and rate limits are measured against.

The CR-01 policy could judge a single order in isolation — is this symbol allowed, is this
notional within the per-order cap. Session limits cannot work that way: a daily loss limit,
a drawdown limit, a turnover budget and an order-rate cap are all statements about
*accumulated* activity, so something has to accumulate it.

That something is :class:`RiskMetrics`. It is deliberately a plain, deterministic
accumulator with no I/O and no clock of its own: it is fed from journaled events, so
rebuilding it from a journal reproduces exactly the state the live process had. If the
metrics were derived from the broker or from wall-clock time, a replay could not reproduce a
halt decision, and the reason for a halt would be unrecoverable.

Design choices worth stating:

* **Peak equity is monotonic.** Drawdown is measured from the highest equity ever observed in
  the session, so it cannot be reset by a losing streak followed by a partial recovery.
* **Turnover counts notional traded, not orders.** Ten small orders and one large order are
  very different risks; counting orders would let a strategy churn.
* **The day boundary comes from an injected date, never from the clock.** A session that
  crosses midnight in UTC but not in exchange-local time must not silently reset a loss
  limit, so the caller supplies the trading day explicitly.
* **Everything is `Decimal`.** A loss limit evaluated in binary floating point can be wrong
  in the last cent, and a limit that is wrong in the last cent is a limit that can be
  exceeded by exactly that much.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.canonical import digest_of
from tigerquant.core.clock import ensure_utc
from tigerquant.core.money import ZERO, q_cash, to_decimal

if TYPE_CHECKING:
    from tigerquant.core.enums import OrderSide

__all__ = ["RiskMetrics", "RiskMetricsSnapshot"]


@dataclass(frozen=True, slots=True)
class RiskMetricsSnapshot:
    """Immutable view of the metrics, for recording alongside a risk outcome."""

    trading_day: dt.date
    session_start_equity: Decimal
    peak_equity: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    gross_exposure: Decimal
    traded_notional_today: Decimal
    orders_today: int
    orders_last_hour: int
    drawdown_fraction: Decimal
    daily_loss_fraction: Decimal
    turnover_fraction: Decimal

    def digest(self) -> str:
        """Content digest, for recording alongside a risk outcome.

        Serialised through :func:dataclasses.asdict because this is a plain dataclass and
        `core.canonical` deliberately understands only primitives — teaching it about
        arbitrary dataclasses would widen what has to be deterministic.
        """
        return digest_of(asdict(self))


@dataclass
class RiskMetrics:
    """Accumulated session risk quantities.

    Fed by the pipeline from journaled events (fills and marks). Deterministic: the same
    event sequence produces the same metrics, so a replay reproduces any halt exactly.
    """

    #: Equity at the start of the trading day; the denominator for the daily loss limit.
    session_start_equity: Decimal = ZERO
    peak_equity: Decimal = ZERO
    equity: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    gross_exposure: Decimal = ZERO
    traded_notional_today: Decimal = ZERO
    trading_day: dt.date | None = None
    _order_times: list[dt.datetime] = field(default_factory=list, repr=False)
    _orders_today: int = 0

    # -- construction ------------------------------------------------------------

    @classmethod
    def baseline(cls, *, equity: Decimal | str) -> RiskMetrics:
        """Metrics with the session opened at `equity` and no day boundary set.

        For a backtest, a single-session run, or a test: the loss, drawdown and turnover
        limits all need an opening equity to measure against, and this supplies one without
        inventing a trading day. A multi-day paper worker should call
        :meth:start_session with the exchange trading day instead, so the daily counters
        roll.
        """
        value = q_cash(to_decimal(equity))
        metrics = cls()
        metrics.session_start_equity = value
        metrics.peak_equity = value
        metrics.equity = value
        return metrics

    # -- session lifecycle -------------------------------------------------------

    def start_session(self, *, trading_day: dt.date, equity: Decimal | str) -> None:
        """Begin a trading day. Resets the daily counters and re-bases the loss limit."""
        value = q_cash(to_decimal(equity))
        self.trading_day = trading_day
        self.session_start_equity = value
        self.peak_equity = max(value, ZERO)
        self.equity = value
        self.traded_notional_today = ZERO
        self._orders_today = 0
        self._order_times.clear()

    def roll_day_if_needed(self, *, trading_day: dt.date, equity: Decimal | str) -> bool:
        """Start a new session when the trading day changes. Returns whether it rolled.

        The day is supplied by the caller rather than read from a clock, because the
        trading day is an exchange-calendar fact (see ``market.calendar``), not a wall-clock
        one.
        """
        if self.trading_day == trading_day:
            return False
        self.start_session(trading_day=trading_day, equity=equity)
        return True

    # -- updates -----------------------------------------------------------------

    def record_valuation(
        self,
        *,
        equity: Decimal | str,
        gross_exposure: Decimal | str,
        realized_pnl: Decimal | str,
        unrealized_pnl: Decimal | str,
    ) -> None:
        """Update the marked state. Peak equity only ever rises."""
        self.equity = q_cash(to_decimal(equity))
        self.gross_exposure = q_cash(to_decimal(gross_exposure))
        self.realized_pnl = q_cash(to_decimal(realized_pnl))
        self.unrealized_pnl = q_cash(to_decimal(unrealized_pnl))
        self.peak_equity = max(self.peak_equity, self.equity)

    def record_order_submitted(self, *, at: dt.datetime, notional: Decimal | str) -> None:
        """Record a submission: it counts toward turnover and the order-rate limits."""
        moment = ensure_utc(at)
        self._order_times.append(moment)
        self._orders_today += 1
        self.traded_notional_today = q_cash(self.traded_notional_today + abs(to_decimal(notional)))
        if self.trading_day is None:
            # No session has been started, so the caller is using this as a bare counter.
            # Annotating the day from the first submission keeps the metrics coherent
            # without inventing a session start equity, which the loss limits need and
            # would refuse to evaluate without.
            self.trading_day = moment.date()

    def record_fill(self, *, notional: Decimal | str, side: OrderSide | str) -> None:
        """Record a fill's notional as turnover.

        Turnover is measured on *executed* notional as well as submitted notional; the
        submitted figure guards against a strategy that submits without filling, and the
        executed figure is what actually changed the portfolio. Both are accumulated into
        the same budget, so neither can be used to evade the limit.
        """
        del side  # both sides consume turnover; direction is a separate concern
        self.traded_notional_today = q_cash(self.traded_notional_today + abs(to_decimal(notional)))

    # -- derived quantities ------------------------------------------------------

    @property
    def orders_today(self) -> int:
        return self._orders_today

    def orders_last_hour(self, *, now: dt.datetime | None = None) -> int:
        """Submissions within the hour ending at ``now`` (or at the latest submission)."""
        if not self._order_times:
            return 0
        reference = ensure_utc(now) if now is not None else self._order_times[-1]
        cutoff = reference - dt.timedelta(hours=1)
        return sum(1 for moment in self._order_times if moment > cutoff)

    def daily_loss_fraction(self) -> Decimal:
        """Loss from the session's opening equity, as a positive fraction.

        A gain returns zero rather than a negative "loss", so a limit comparison is
        ``loss > limit`` in both directions.
        """
        if self.session_start_equity <= ZERO:
            return ZERO
        loss = self.session_start_equity - self.equity
        return ZERO if loss <= ZERO else loss / self.session_start_equity

    def drawdown_fraction(self) -> Decimal:
        """Fall from the session's peak equity, as a positive fraction."""
        if self.peak_equity <= ZERO:
            return ZERO
        fall = self.peak_equity - self.equity
        return ZERO if fall <= ZERO else fall / self.peak_equity

    def turnover_fraction(self) -> Decimal:
        """Traded notional divided by the session's opening equity."""
        if self.session_start_equity <= ZERO:
            return ZERO
        return self.traded_notional_today / self.session_start_equity

    def snapshot(self) -> RiskMetricsSnapshot:
        """Freeze the current values for recording alongside a risk outcome."""
        return RiskMetricsSnapshot(
            trading_day=self.trading_day or dt.date(1970, 1, 1),
            session_start_equity=self.session_start_equity,
            peak_equity=self.peak_equity,
            equity=self.equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            gross_exposure=self.gross_exposure,
            traded_notional_today=self.traded_notional_today,
            orders_today=self._orders_today,
            orders_last_hour=self.orders_last_hour(),
            drawdown_fraction=self.drawdown_fraction(),
            daily_loss_fraction=self.daily_loss_fraction(),
            turnover_fraction=self.turnover_fraction(),
        )

    def describe(self) -> str:
        return (
            f"equity {self.equity} (start {self.session_start_equity}, peak {self.peak_equity}), "
            f"drawdown {self.drawdown_fraction():.4f}, "
            f"daily loss {self.daily_loss_fraction():.4f}, "
            f"turnover {self.turnover_fraction():.4f}, "
            f"orders today {self._orders_today}, last hour {self.orders_last_hour()}"
        )
