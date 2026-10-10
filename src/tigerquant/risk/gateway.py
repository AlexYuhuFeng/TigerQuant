"""The risk gateway — the only component that can allow an order.

Design rules that make this an authority rather than a filter (ADR-0007):

* **Deny by default.** An order is allowed only when *every* check passes. Any exception
  raised inside a check is converted into a DENY, so a bug cannot become a bypass.
* **Pure evaluation.** :meth:`RiskGateway.evaluate` performs no I/O and calls no model; a
  provider outage cannot change its answer (acceptance gate 11).
* **Capability issuance.** Only :meth:`RiskGateway.issue` mints an
  :class:`~tigerquant.risk.approvals.ApprovedOrder`, and the broker adapter accepts
  nothing else.
* **Traceable.** Every outcome carries the policy hash and the portfolio digest it was
  computed against, so the decision can be reconstructed later.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import (
    Mode,
    OrderSide,
    ReasonCode,
    RiskOutcomeKind,
    SubmissionTarget,
    capabilities_for,
)
from tigerquant.core.errors import RiskDenied
from tigerquant.core.money import ZERO, to_decimal
from tigerquant.portfolio.sizing import notional
from tigerquant.risk.approvals import ApprovalRegistry, ApprovedOrder, OrderIntentView
from tigerquant.risk.state import RiskState

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from tigerquant.risk.metrics import RiskMetrics
    from tigerquant.risk.policy import PortfolioView, RiskPolicy, TradingSessionView

__all__ = ["RiskGateway", "RiskOutcome", "RiskRequest"]


@dataclass(frozen=True, slots=True)
class RiskRequest:
    """Everything the gateway needs, supplied by the caller but never trusted blindly."""

    order: OrderIntentView
    marks: Mapping[str, Decimal]
    mark_available_at: Mapping[str, dt.datetime]
    as_of: dt.datetime
    startup_gate_open: bool
    orders_this_cycle: int
    snapshot_usable: bool
    session: TradingSessionView | None = None
    #: Number of intents in an unreconciled/partially-filled state. Anything above zero
    #: blocks new exposure (ADR-0008 §4).
    blocking_intents: int = 0
    #: Where the order would physically go. Defaults to NONE so that a caller which
    #: forgets to declare a target gets a denial rather than a submission.
    submission_target: SubmissionTarget = SubmissionTarget.NONE
    mode: Mode = Mode.BACKTEST
    #: Accumulated session risk quantities (CR-07). ``None`` means the caller supplied none,
    #: and the session limits then fail closed: a loss limit that cannot be evaluated is not
    #: a limit that may be assumed satisfied.
    metrics: RiskMetrics | None = None

    def mark_for(self, instrument: str) -> Decimal | None:
        mark = self.marks.get(instrument)
        return None if mark is None else to_decimal(mark)


@dataclass(frozen=True, slots=True)
class RiskOutcome:
    """The verdict, with enough context to reconstruct it."""

    intent_id: str
    kind: RiskOutcomeKind
    reason_codes: tuple[ReasonCode, ...]
    checks_run: tuple[str, ...]
    policy_hash: str
    portfolio_digest: str
    detail: str
    approved_order: ApprovedOrder | None = None

    @property
    def allowed(self) -> bool:
        return self.kind is RiskOutcomeKind.ALLOW

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise RiskDenied(
                f"risk denied intent {self.intent_id}: {self.detail}", self.reason_codes
            )


@dataclass(frozen=True, slots=True)
class _Context:
    request: RiskRequest
    policy: RiskPolicy
    portfolio: PortfolioView
    state: RiskState
    equity: Decimal
    gross_exposure: Decimal
    current_quantity: int
    order_notional: Decimal
    projected_quantity: int
    projected_symbol_value: Decimal
    projected_gross: Decimal


_CheckResult = tuple[ReasonCode, str] | None


class RiskGateway:
    """Deterministic, fail-closed policy enforcement."""

    __slots__ = ("_policy", "_registry", "_state")

    def __init__(
        self,
        *,
        policy: RiskPolicy,
        state: RiskState,
        registry: ApprovalRegistry,
    ) -> None:
        self._policy = policy
        self._state = state
        self._registry = registry

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def registry(self) -> ApprovalRegistry:
        return self._registry

    # -- public API --------------------------------------------------------------

    def evaluate(self, request: RiskRequest, *, portfolio: PortfolioView) -> RiskOutcome:
        """Evaluate one intent. Pure: no I/O, no side effects, no model calls."""
        checks_run: list[str] = []
        reasons: list[ReasonCode] = []
        details: list[str] = []
        policy_hash = self._policy.content_hash()
        try:
            portfolio_digest = portfolio.digest()
        except Exception:  # noqa: BLE001 - an unhashable portfolio must not abort the check
            portfolio_digest = "unavailable"

        context: _Context | None = None
        try:
            context = self._build_context(request, portfolio)
        except Exception as exc:  # noqa: BLE001 - any failure must fail closed (ADR-0007 §2)
            # A portfolio that cannot be valued is a denial, never an assumption.
            reasons.append(ReasonCode.CHECK_ERROR)
            details.append(f"portfolio valuation failed: {type(exc).__name__}: {exc}")

        for name, check in self._checks():
            checks_run.append(name)
            if context is None:
                continue
            try:
                failure = check(context)
            except Exception as exc:  # noqa: BLE001 - any failure must fail closed
                failure = (ReasonCode.CHECK_ERROR, f"{name} raised {type(exc).__name__}: {exc}")
            if failure is not None:
                code, detail = failure
                if code not in reasons:
                    reasons.append(code)
                details.append(detail)

        kind = RiskOutcomeKind.DENY if reasons else RiskOutcomeKind.ALLOW
        detail = "; ".join(details) if details else "all checks passed"
        return RiskOutcome(
            intent_id=request.order.intent_id,
            kind=kind,
            reason_codes=tuple(reasons),
            checks_run=tuple(checks_run),
            policy_hash=policy_hash,
            portfolio_digest=portfolio_digest,
            detail=detail,
        )

    def issue(
        self,
        outcome: RiskOutcome,
        *,
        order: OrderIntentView,
        portfolio_digest: str,
        approved_at: dt.datetime,
        risk_event_id: str,
    ) -> ApprovedOrder:
        """Mint the single-use approval for an allowed outcome."""
        if not outcome.allowed:
            msg = f"cannot issue an approval for a denied outcome ({outcome.intent_id})"
            raise RiskDenied(msg, outcome.reason_codes or (ReasonCode.CHECK_ERROR,))
        return self._registry.issue(
            order=order,
            policy_hash=outcome.policy_hash,
            portfolio_digest=portfolio_digest,
            approved_at=ensure_utc(approved_at),
            risk_event_id=risk_event_id,
        )

    # -- context -----------------------------------------------------------------

    def _build_context(self, request: RiskRequest, portfolio: PortfolioView) -> _Context:
        marks: dict[str, Decimal] = dict(request.marks)
        equity = portfolio.equity(marks)
        gross = portfolio.gross_exposure(marks)
        instrument = request.order.instrument
        current_quantity = portfolio.position(instrument).quantity
        mark = request.mark_for(instrument)

        # A missing mark for the *order's* instrument is not a context failure: it is a
        # denial raised by ``mark_present_and_fresh`` / ``mark_price_floor``. Projections
        # are left at zero so those checks are reached and reported by name.
        if mark is None:
            order_notional = ZERO
            projected_symbol_value = ZERO
            projected_gross = gross
            projected_quantity = current_quantity
        else:
            order_notional = notional(quantity=request.order.quantity, price=mark)
            delta = (
                request.order.quantity
                if request.order.side is OrderSide.BUY
                else -request.order.quantity
            )
            projected_quantity = current_quantity + delta
            current_value = current_quantity * mark
            projected_symbol_value = projected_quantity * mark
            projected_gross = gross - abs(current_value) + abs(projected_symbol_value)

        return _Context(
            request=request,
            policy=self._policy,
            portfolio=portfolio,
            state=self._state,
            equity=equity,
            gross_exposure=gross,
            current_quantity=current_quantity,
            order_notional=order_notional,
            projected_quantity=projected_quantity,
            projected_symbol_value=projected_symbol_value,
            projected_gross=projected_gross,
        )

    # -- checks ------------------------------------------------------------------

    def _checks(self) -> tuple[tuple[str, Callable[[_Context], _CheckResult]], ...]:
        return (
            ("mode_capability", _check_mode_capability),
            ("startup_gate", _check_startup_gate),
            ("risk_state", _check_risk_state),
            ("snapshot_usable", _check_snapshot),
            ("instrument_allowed", _check_instrument),
            ("quantity_positive", _check_quantity),
            ("market_open", _check_market_open),
            ("instrument_not_halted", _check_halted),
            ("mark_present_and_fresh", _check_mark_freshness),
            ("mark_price_floor", _check_mark_price),
            ("short_not_allowed", _check_no_short),
            ("buying_power", _check_buying_power),
            ("max_order_fraction", _check_order_fraction),
            ("max_symbol_fraction", _check_symbol_fraction),
            ("gross_exposure", _check_gross_exposure),
            ("order_rate", _check_order_rate),
            ("no_blocking_intents", _check_no_blocking_intents),
            # Session limits (CR-07). Appended rather than reordered, so the recorded check
            # sequence stays stable across policy versions.
            ("daily_loss", _check_daily_loss),
            ("drawdown", _check_drawdown),
            ("turnover", _check_turnover),
            ("order_rate_rolling", _check_rolling_order_rate),
        )


# -- individual checks -----------------------------------------------------------
# Each returns ``None`` to pass, or a (ReasonCode, detail) pair to deny.


def _check_mode_capability(ctx: _Context) -> _CheckResult:
    """Verify that this mode may submit to this kind of target.

    The gateway re-derives the permission from ``(mode, submission_target)`` instead of
    trusting the caller's ``can_submit`` flag, so a bug upstream cannot widen access.
    """
    target = ctx.request.submission_target
    caps = capabilities_for(ctx.request.mode)
    if target is SubmissionTarget.NONE:
        return (
            ReasonCode.MODE_CAPABILITY_ABSENT,
            f"mode {ctx.request.mode.value} resolves to submission target NONE",
        )
    if target is SubmissionTarget.BROKER and not caps.can_submit_orders:
        return (
            ReasonCode.MODE_CAPABILITY_ABSENT,
            f"mode {ctx.request.mode.value} has no broker-submission capability",
        )
    if target is SubmissionTarget.SIMULATION and ctx.request.mode not in {
        Mode.BACKTEST,
        Mode.SHADOW,
    }:
        return (
            ReasonCode.MODE_CAPABILITY_ABSENT,
            f"mode {ctx.request.mode.value} may not use a simulated execution target; "
            "a paper run must exercise the real paper venue",
        )
    return None


def _check_startup_gate(ctx: _Context) -> _CheckResult:
    if not ctx.request.startup_gate_open:
        return (
            ReasonCode.STARTUP_GATE_CLOSED,
            "startup reconciliation has not completed; new exposure is blocked",
        )
    return None


def _check_risk_state(ctx: _Context) -> _CheckResult:
    if ctx.state.blocks_new_exposure():
        state = "halted" if ctx.state.halted else "frozen"
        reason = ctx.state.reason_code.value if ctx.state.reason_code else "unknown"
        return (ReasonCode.RISK_HALTED, f"risk state is {state} ({reason})")
    return None


def _check_snapshot(ctx: _Context) -> _CheckResult:
    if not ctx.request.snapshot_usable:
        return (
            ReasonCode.SNAPSHOT_INCOMPLETE,
            "the decision carries no usable point-in-time evidence snapshot",
        )
    return None


def _check_instrument(ctx: _Context) -> _CheckResult:
    instrument = ctx.request.order.instrument
    if not ctx.policy.is_allowed(instrument):
        return (
            ReasonCode.SYMBOL_NOT_ALLOWED,
            f"{instrument} is not in the approved universe {list(ctx.policy.allowed_instruments)}",
        )
    return None


def _check_quantity(ctx: _Context) -> _CheckResult:
    if ctx.request.order.quantity <= 0:
        return (ReasonCode.ZERO_QUANTITY, "order quantity must be positive")
    return None


def _check_market_open(ctx: _Context) -> _CheckResult:
    session = ctx.request.session
    if session is None:
        return (ReasonCode.MARKET_CLOSED, "no exchange calendar is configured; refusing to trade")
    if not session.is_open(ctx.request.as_of):
        return (
            ReasonCode.MARKET_CLOSED,
            f"market is closed at {ctx.request.as_of.isoformat()}",
        )
    # The approved order window is narrower than the session: we do not trade the
    # opening or closing auction without a documented, separately approved policy.
    if not session.is_within_approved_window(ctx.request.as_of):
        return (
            ReasonCode.MARKET_CLOSED,
            f"{ctx.request.as_of.isoformat()} is outside the approved order window",
        )
    return None


def _check_halted(ctx: _Context) -> _CheckResult:
    session = ctx.request.session
    if session is not None and session.is_halted(ctx.request.order.instrument, ctx.request.as_of):
        return (
            ReasonCode.INSTRUMENT_HALTED,
            f"{ctx.request.order.instrument} is halted",
        )
    return None


def _check_mark_freshness(ctx: _Context) -> _CheckResult:
    instrument = ctx.request.order.instrument
    available_at = ctx.request.mark_available_at.get(instrument)
    if available_at is None:
        return (ReasonCode.MARK_MISSING, f"no mark price recorded for {instrument}")
    age = (ctx.request.as_of - ensure_utc(available_at)).total_seconds()
    if age < 0:
        return (
            ReasonCode.MARK_STALE,
            f"mark for {instrument} is dated in the future by {-age:.0f}s (clock skew)",
        )
    if age > ctx.policy.max_mark_age_seconds:
        return (
            ReasonCode.MARK_STALE,
            f"mark for {instrument} is {age:.0f}s old; limit is {ctx.policy.max_mark_age_seconds}s",
        )
    return None


def _check_mark_price(ctx: _Context) -> _CheckResult:
    mark = ctx.request.mark_for(ctx.request.order.instrument)
    if mark is None:
        return (ReasonCode.MARK_MISSING, "no mark price available")
    if mark < ctx.policy.min_mark_price:
        return (
            ReasonCode.NON_POSITIVE_PRICE,
            f"mark {mark} is below the policy floor {ctx.policy.min_mark_price}",
        )
    return None


def _check_no_short(ctx: _Context) -> _CheckResult:
    if ctx.request.order.side is OrderSide.SELL and ctx.projected_quantity < 0:
        return (
            ReasonCode.SHORT_NOT_ALLOWED,
            f"order would leave a short position of {ctx.projected_quantity} shares "
            f"(holding {ctx.current_quantity})",
        )
    return None


def _check_buying_power(ctx: _Context) -> _CheckResult:
    if ctx.request.order.side is not OrderSide.BUY:
        return None
    if ctx.order_notional > ctx.portfolio.cash:
        return (
            ReasonCode.INSUFFICIENT_BUYING_POWER,
            f"order notional {ctx.order_notional} exceeds cash {ctx.portfolio.cash} "
            "(margin is not permitted)",
        )
    return None


def _check_order_fraction(ctx: _Context) -> _CheckResult:
    if ctx.equity <= ZERO:
        return (ReasonCode.CHECK_ERROR, "portfolio equity is not positive; cannot size an order")
    fraction = ctx.order_notional / ctx.equity
    if fraction > ctx.policy.max_order_fraction:
        return (
            ReasonCode.MAX_ORDER_FRACTION_EXCEEDED,
            f"order is {fraction:.6f} of equity; limit is {ctx.policy.max_order_fraction}",
        )
    return None


def _check_symbol_fraction(ctx: _Context) -> _CheckResult:
    if ctx.equity <= ZERO:
        return (ReasonCode.CHECK_ERROR, "portfolio equity is not positive")
    fraction = abs(ctx.projected_symbol_value) / ctx.equity
    if fraction > ctx.policy.max_symbol_fraction:
        return (
            ReasonCode.MAX_SYMBOL_FRACTION_EXCEEDED,
            f"post-trade weight of {ctx.request.order.instrument} would be {fraction:.6f}; "
            f"limit is {ctx.policy.max_symbol_fraction}",
        )
    return None


def _check_gross_exposure(ctx: _Context) -> _CheckResult:
    if ctx.equity <= ZERO:
        return (ReasonCode.CHECK_ERROR, "portfolio equity is not positive")
    fraction = ctx.projected_gross / ctx.equity
    if fraction > ctx.policy.gross_exposure_fraction:
        return (
            ReasonCode.GROSS_EXPOSURE_EXCEEDED,
            f"post-trade gross exposure would be {fraction:.6f} of equity; limit is "
            f"{ctx.policy.gross_exposure_fraction}",
        )
    return None


def _check_order_rate(ctx: _Context) -> _CheckResult:
    if ctx.request.orders_this_cycle >= ctx.policy.max_orders_per_cycle:
        return (
            ReasonCode.ORDER_RATE_EXCEEDED,
            f"already submitted {ctx.request.orders_this_cycle} orders this cycle; "
            f"limit is {ctx.policy.max_orders_per_cycle}",
        )
    return None


def _check_no_blocking_intents(ctx: _Context) -> _CheckResult:
    """An unreconciled or partially filled order blocks all new exposure.

    Without this, a system that lost track of one submission would happily open a second
    position while the first is still unresolved (ADR-0008 §3-4).
    """
    if ctx.request.blocking_intents > 0:
        return (
            ReasonCode.UNRECONCILED,
            f"{ctx.request.blocking_intents} order intent(s) are unresolved "
            "(unknown outcome, quarantined, partially filled or cancelling); "
            "new exposure is blocked until reconciliation completes",
        )
    return None


def _check_daily_loss(ctx: _Context) -> _CheckResult:
    """Refuse new exposure once the session has lost too much of its opening equity.

    Deliberately one-sided: a session that is *up* is not a session with a negative loss, so
    the comparison is ``loss > limit`` and never ``abs(pnl) > limit``.
    """
    metrics = ctx.request.metrics
    if metrics is None:
        return (
            ReasonCode.RISK_HALTED,
            "no session metrics are available, so the daily loss limit cannot be evaluated; "
            "refusing to open exposure rather than assuming the limit is satisfied",
        )
    loss = metrics.daily_loss_fraction()
    if loss > ctx.policy.max_daily_loss_fraction:
        return (
            ReasonCode.DAILY_LOSS_EXCEEDED,
            f"session loss is {loss:.6f} of opening equity; limit is "
            f"{ctx.policy.max_daily_loss_fraction}",
        )
    return None


def _check_drawdown(ctx: _Context) -> _CheckResult:
    """Refuse new exposure once equity is far enough below its session peak.

    Measured from the peak, so a partial recovery does not reset the limit.
    """
    metrics = ctx.request.metrics
    if metrics is None:
        return (
            ReasonCode.RISK_HALTED,
            "no session metrics are available, so the drawdown limit cannot be evaluated",
        )
    drawdown = metrics.drawdown_fraction()
    if drawdown > ctx.policy.max_drawdown_fraction:
        return (
            ReasonCode.DRAWDOWN_EXCEEDED,
            f"drawdown is {drawdown:.6f} from the session peak; limit is "
            f"{ctx.policy.max_drawdown_fraction}",
        )
    return None


def _check_turnover(ctx: _Context) -> _CheckResult:
    """Refuse once the session has traded too much notional.

    Turnover counts *notional*, not order count: ten small orders and one large order carry
    very different risk, and an order-count limit would let a strategy churn.
    """
    metrics = ctx.request.metrics
    if metrics is None:
        return (
            ReasonCode.RISK_HALTED,
            "no session metrics are available, so the turnover limit cannot be evaluated",
        )
    turnover = metrics.turnover_fraction()
    if turnover > ctx.policy.max_turnover_fraction:
        return (
            ReasonCode.TURNOVER_EXCEEDED,
            f"session turnover is {turnover:.6f} of opening equity; limit is "
            f"{ctx.policy.max_turnover_fraction}",
        )
    return None


def _check_rolling_order_rate(ctx: _Context) -> _CheckResult:
    """Bound submissions per rolling hour, complementing the per-cycle cap.

    ``max_orders_per_cycle`` bounds one pipeline cycle; without a rolling bound, a strategy
    that cycles quickly can still submit without limit.
    """
    metrics = ctx.request.metrics
    if metrics is None:
        return (
            ReasonCode.RISK_HALTED,
            "no session metrics are available, so the rolling order-rate limit cannot be evaluated",
        )
    recent = metrics.orders_last_hour(now=ctx.request.as_of)
    if recent > ctx.policy.max_orders_per_hour:
        return (
            ReasonCode.ORDER_RATE_EXCEEDED,
            f"{recent} submissions in the last hour; limit is {ctx.policy.max_orders_per_hour}",
        )
    return None
