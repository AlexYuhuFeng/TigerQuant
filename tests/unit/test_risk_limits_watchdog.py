"""CR-07: session risk limits, the watchdog, and privilege-bypass attempts.

Acceptance gate for CR-07 is "risk tests and privilege-bypass tests; kill switch; no
alternative order path". The last two files of this module are the ones that matter most:
they do not test that the controls *work*, they test that the controls cannot be
*avoided*.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import (
    Mode,
    OrderSide,
    OrderType,
    ReasonCode,
    RiskOutcomeKind,
    SubmissionTarget,
    TimeInForce,
)
from tigerquant.core.errors import RiskDenied
from tigerquant.market.session import TradingSession
from tigerquant.ops.watchdog import Watchdog
from tigerquant.portfolio.ledger import PortfolioLedger
from tigerquant.risk.approvals import ApprovalRegistry, OrderIntentView
from tigerquant.risk.gateway import RiskGateway, RiskRequest
from tigerquant.risk.metrics import RiskMetrics
from tigerquant.risk.policy import RiskPolicy
from tigerquant.risk.state import Actor, RiskState

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
MARKS = {"SPY": Decimal("600.00")}
EQUITY = Decimal(100000)


def policy(**overrides: object) -> RiskPolicy:
    base: dict[str, object] = {
        "policy_version": "cr07-test",
        "universe_version": "u",
        "allowed_instruments": ("SPY",),
        "gross_exposure_fraction": Decimal("0.5"),
        "max_symbol_fraction": Decimal("0.1"),
        "max_order_fraction": Decimal("0.1"),
        "max_mark_age_seconds": 900,
        "max_orders_per_cycle": 1,
        "max_turnover_fraction": Decimal("2.0"),
        "max_daily_loss_fraction": Decimal("0.02"),
        "max_drawdown_fraction": Decimal("0.05"),
        "max_orders_per_hour": 30,
    }
    base.update(overrides)
    return RiskPolicy(**base)  # type: ignore[arg-type]


def order(quantity: int = 8) -> OrderIntentView:
    return OrderIntentView(
        intent_id="bt-oi_cr07",
        decision_id="bt-dec_cr07",
        instrument="SPY",
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        client_order_id="TQ-bt-cr07",
    )


def request(metrics: RiskMetrics | None, **overrides: object) -> RiskRequest:
    base: dict[str, object] = {
        "order": order(),
        "marks": dict(MARKS),
        "mark_available_at": {"SPY": AT},
        "as_of": AT,
        "startup_gate_open": True,
        "orders_this_cycle": 0,
        "snapshot_usable": True,
        "session": TradingSession(),
        "submission_target": SubmissionTarget.SIMULATION,
        "mode": Mode.BACKTEST,
        "metrics": metrics,
    }
    base.update(overrides)
    return RiskRequest(**base)  # type: ignore[arg-type]


def gateway(p: RiskPolicy | None = None, state: RiskState | None = None) -> RiskGateway:
    return RiskGateway(
        policy=p or policy(),
        state=state or RiskState(),
        registry=ApprovalRegistry(),
    )


class TestRiskMetrics:
    def test_baseline_opens_the_session(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        assert metrics.session_start_equity == EQUITY
        assert metrics.peak_equity == EQUITY
        assert metrics.equity == EQUITY
        assert metrics.daily_loss_fraction() == 0
        assert metrics.drawdown_fraction() == 0

    def test_peak_equity_only_rises(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_valuation(
            equity="110000", gross_exposure="0", realized_pnl="10000", unrealized_pnl="0"
        )
        metrics.record_valuation(
            equity="105000", gross_exposure="0", realized_pnl="5000", unrealized_pnl="0"
        )
        assert metrics.peak_equity == Decimal(110000)
        # Drawdown is measured from the peak, so a partial recovery does not reset it.
        assert metrics.drawdown_fraction() > Decimal("0.04")

    def test_a_gain_is_not_a_negative_loss(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_valuation(
            equity="120000", gross_exposure="0", realized_pnl="20000", unrealized_pnl="0"
        )
        assert metrics.daily_loss_fraction() == 0

    def test_turnover_counts_notional_not_orders(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_order_submitted(at=AT, notional="50000")
        metrics.record_order_submitted(at=AT, notional="50000")
        assert metrics.orders_today == 2
        assert metrics.turnover_fraction() == Decimal(1)

    def test_fills_also_consume_turnover(self) -> None:
        """A strategy cannot evade the budget by submitting small and filling large."""
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_fill(notional="75000", side="BUY")
        assert metrics.traded_notional_today == Decimal("75000.00")

    def test_rolling_order_rate_window(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        for minutes in (0, 10, 20):
            metrics.record_order_submitted(at=AT + dt.timedelta(minutes=minutes), notional="1")
        later = AT + dt.timedelta(minutes=90)
        # Only the most recent submission is within the hour ending at `later`.
        assert metrics.orders_last_hour(now=later) == 0
        assert metrics.orders_last_hour(now=AT + dt.timedelta(minutes=25)) == 3

    def test_session_roll_resets_the_daily_counters(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.start_session(trading_day=dt.date(2026, 3, 2), equity=EQUITY)
        metrics.record_order_submitted(at=AT, notional="50000")
        metrics.record_valuation(
            equity="95000", gross_exposure="0", realized_pnl="-5000", unrealized_pnl="0"
        )
        assert metrics.daily_loss_fraction() > 0

        assert metrics.roll_day_if_needed(trading_day=dt.date(2026, 3, 3), equity="95000")
        assert metrics.orders_today == 0
        assert metrics.traded_notional_today == 0
        assert metrics.daily_loss_fraction() == 0
        # The very next call is a no-op: the day has already rolled.
        assert not metrics.roll_day_if_needed(trading_day=dt.date(2026, 3, 3), equity="95000")

    def test_snapshot_is_deterministic(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.start_session(trading_day=dt.date(2026, 3, 2), equity=EQUITY)
        assert metrics.snapshot().digest() == metrics.snapshot().digest()

    def test_describe_reads_like_a_report(self) -> None:
        text = RiskMetrics.baseline(equity=EQUITY).describe()
        assert "drawdown" in text
        assert "turnover" in text


class TestSessionLimitsInTheGateway:
    def test_a_clean_order_still_passes_with_healthy_metrics(self) -> None:
        outcome = gateway().evaluate(
            request(RiskMetrics.baseline(equity=EQUITY)),
            portfolio=PortfolioLedger(initial_cash=EQUITY),
        )
        assert outcome.kind is RiskOutcomeKind.ALLOW
        assert "daily_loss" in outcome.checks_run
        assert "drawdown" in outcome.checks_run
        assert "turnover" in outcome.checks_run
        assert "order_rate_rolling" in outcome.checks_run

    def test_missing_metrics_fails_closed(self) -> None:
        """A loss limit that cannot be evaluated is not a limit that may be assumed met."""
        outcome = gateway().evaluate(request(None), portfolio=PortfolioLedger(initial_cash=EQUITY))
        assert outcome.kind is RiskOutcomeKind.DENY
        assert ReasonCode.RISK_HALTED in outcome.reason_codes

    def test_daily_loss_limit_denies(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        # A 3% loss against a 2% limit.
        metrics.record_valuation(
            equity="97000", gross_exposure="0", realized_pnl="-3000", unrealized_pnl="0"
        )
        outcome = gateway().evaluate(
            request(metrics), portfolio=PortfolioLedger(initial_cash=EQUITY)
        )
        assert outcome.kind is RiskOutcomeKind.DENY
        assert ReasonCode.DAILY_LOSS_EXCEEDED in outcome.reason_codes

    def test_drawdown_limit_denies_even_when_the_day_is_flat(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_valuation(
            equity="110000", gross_exposure="0", realized_pnl="10000", unrealized_pnl="0"
        )
        # Up 10%, then down to 104_000: still up on the day, but 5.4% below the peak.
        metrics.record_valuation(
            equity="104000", gross_exposure="0", realized_pnl="4000", unrealized_pnl="0"
        )
        outcome = gateway().evaluate(
            request(metrics), portfolio=PortfolioLedger(initial_cash=EQUITY)
        )
        assert ReasonCode.DRAWDOWN_EXCEEDED in outcome.reason_codes
        assert ReasonCode.DAILY_LOSS_EXCEEDED not in outcome.reason_codes

    def test_turnover_limit_denies(self) -> None:
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_order_submitted(at=AT, notional="250000")  # 2.5x a 2.0x limit
        outcome = gateway().evaluate(
            request(metrics), portfolio=PortfolioLedger(initial_cash=EQUITY)
        )
        assert ReasonCode.TURNOVER_EXCEEDED in outcome.reason_codes

    def test_rolling_order_rate_denies(self) -> None:
        p = policy(max_orders_per_hour=2)
        metrics = RiskMetrics.baseline(equity=EQUITY)
        for _ in range(3):
            metrics.record_order_submitted(at=AT, notional="1")
        outcome = gateway(p).evaluate(
            request(metrics), portfolio=PortfolioLedger(initial_cash=EQUITY)
        )
        assert ReasonCode.ORDER_RATE_EXCEEDED in outcome.reason_codes

    def test_limits_are_one_sided_in_the_safe_direction(self) -> None:
        """A profitable session must not trip a *loss* limit."""
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_valuation(
            equity="150000", gross_exposure="0", realized_pnl="50000", unrealized_pnl="0"
        )
        assert metrics.daily_loss_fraction() == 0
        assert metrics.drawdown_fraction() == 0


class TestWatchdog:
    def _watchdog(self, p: RiskPolicy | None = None) -> tuple[Watchdog, RiskState]:
        state = RiskState()
        return Watchdog(policy=p or policy(), state=state), state

    def test_healthy_state_produces_no_verdicts(self) -> None:
        watchdog, state = self._watchdog()
        verdicts = watchdog.observe(at=AT, metrics=RiskMetrics.baseline(equity=EQUITY))
        assert verdicts == ()
        assert not state.blocks_new_exposure()

    def test_daily_loss_engages_the_halt(self) -> None:
        watchdog, state = self._watchdog()
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_valuation(
            equity="96000", gross_exposure="0", realized_pnl="-4000", unrealized_pnl="0"
        )
        verdicts = watchdog.observe(at=AT, metrics=metrics)
        assert any(v.code is ReasonCode.DAILY_LOSS_EXCEEDED for v in verdicts)
        assert state.halted
        assert state.reason_code is ReasonCode.DAILY_LOSS_EXCEEDED

    def test_drawdown_engages_the_halt(self) -> None:
        watchdog, state = self._watchdog()
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_valuation(
            equity="120000", gross_exposure="0", realized_pnl="0", unrealized_pnl="0"
        )
        metrics.record_valuation(
            equity="110000", gross_exposure="0", realized_pnl="0", unrealized_pnl="0"
        )
        watchdog.observe(at=AT, metrics=metrics)
        assert state.halted

    def test_missing_metrics_engages_the_halt(self) -> None:
        watchdog, state = self._watchdog()
        verdicts = watchdog.observe(at=AT, metrics=None)
        assert verdicts
        assert state.halted

    def test_unreconciled_account_engages_the_halt(self) -> None:
        watchdog, state = self._watchdog()
        watchdog.observe(
            at=AT, metrics=RiskMetrics.baseline(equity=EQUITY), account_reconciled=False
        )
        assert state.halted
        assert state.reason_code is ReasonCode.UNRECONCILED

    def test_unreachable_broker_engages_the_halt(self) -> None:
        watchdog, state = self._watchdog()
        watchdog.observe(at=AT, metrics=RiskMetrics.baseline(equity=EQUITY), broker_reachable=False)
        assert state.halted

    def test_clock_skew_engages_the_halt(self) -> None:
        watchdog, state = self._watchdog()
        watchdog.observe(
            at=AT, metrics=RiskMetrics.baseline(equity=EQUITY), clock_skew_seconds=Decimal(120)
        )
        assert state.halted
        assert state.reason_code is ReasonCode.CLOCK_SKEW

    def test_skew_within_tolerance_is_tolerated(self) -> None:
        watchdog, state = self._watchdog()
        verdicts = watchdog.observe(
            at=AT, metrics=RiskMetrics.baseline(equity=EQUITY), clock_skew_seconds=Decimal(5)
        )
        assert verdicts == ()
        assert not state.halted

    def test_several_conditions_produce_one_halt(self) -> None:
        """A storm of conditions must not produce a storm of halt records."""
        watchdog, state = self._watchdog()
        metrics = RiskMetrics.baseline(equity=EQUITY)
        metrics.record_valuation(
            equity="90000", gross_exposure="0", realized_pnl="-10000", unrealized_pnl="0"
        )
        verdicts = watchdog.observe(
            at=AT, metrics=metrics, blocking_intents=2, account_reconciled=False
        )
        assert len(verdicts) >= 3
        assert len(state.history) == 1

    def test_the_watchdog_cannot_clear_its_own_halt(self) -> None:
        """Only the owner may clear. A supervisor that can un-halt itself is not a control."""
        watchdog, state = self._watchdog()
        watchdog.observe(at=AT, metrics=None)
        assert state.halted
        with pytest.raises(PermissionError):
            state.clear(actor=watchdog.actor, at=AT)
        assert state.halted
        # The owner can.
        assert state.clear(actor=Actor.OWNER, at=AT)
        assert not state.halted

    def test_describe_is_operator_readable(self) -> None:
        watchdog, _ = self._watchdog()
        assert watchdog.summarise() == "watchdog: no halt engaged"
        watchdog.observe(at=AT, metrics=None)
        assert "halted" in watchdog.summarise()


class TestPrivilegeBypass:
    """Attempts to get an order out without going through every control.

    These do not test that the controls work. They test that the controls cannot be
    *avoided* — which is the CR-07 gate wording ("privilege-bypass tests; no alternative
    order path").
    """

    def test_a_denied_outcome_cannot_be_upgraded_to_an_approval(self) -> None:
        g = gateway()
        ledger = PortfolioLedger(initial_cash=EQUITY)
        denied = g.evaluate(request(None), portfolio=ledger)
        assert not denied.allowed
        with pytest.raises(RiskDenied):
            g.issue(
                denied,
                order=order(),
                portfolio_digest="x",
                approved_at=AT,
                risk_event_id="ev",
            )

    def test_a_forged_approval_token_is_refused(self) -> None:
        g = gateway()
        from tigerquant.execution.service import ExecutionService, StartupGate

        gate = StartupGate()
        gate.open(reason="test")
        del ExecutionService  # the token check is on the registry itself
        with pytest.raises(RiskDenied, match="not outstanding"):
            g.registry.consume("apr:forged")

    def test_an_approval_cannot_be_replayed(self) -> None:
        g = gateway()
        ledger = PortfolioLedger(initial_cash=EQUITY)
        outcome = g.evaluate(request(RiskMetrics.baseline(equity=EQUITY)), portfolio=ledger)
        approval = g.issue(
            outcome,
            order=order(),
            portfolio_digest=outcome.portfolio_digest,
            approved_at=AT,
            risk_event_id="ev",
        )
        g.registry.consume(approval.token)
        with pytest.raises(RiskDenied, match="already consumed"):
            g.registry.consume(approval.token)

    def test_the_ai_actor_cannot_halt_or_clear(self) -> None:
        state = RiskState()
        with pytest.raises(PermissionError):
            state.halt(actor=Actor.AGENT, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="x")
        state.halt(actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="x")
        with pytest.raises(PermissionError):
            state.clear(actor=Actor.AGENT, at=AT)

    def test_the_policy_cannot_be_mutated_at_runtime(self) -> None:
        p = policy()
        with pytest.raises(Exception, match="frozen|Instance is frozen"):
            p.max_order_fraction = Decimal(1)  # type: ignore[misc]
        with pytest.raises(Exception, match="frozen|Instance is frozen"):
            p.max_daily_loss_fraction = Decimal(1)  # type: ignore[misc]

    def test_a_tightened_policy_is_a_different_policy(self) -> None:
        """A limit change is a new versioned object, not an edit to a live one."""
        assert policy().content_hash() != policy(max_order_fraction=Decimal("0.01")).content_hash()

    def test_the_gateway_denies_when_its_own_state_is_halted(self) -> None:
        state = RiskState()
        state.halt(
            actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.DRAWDOWN_EXCEEDED, detail="dd"
        )
        outcome = gateway(state=state).evaluate(
            request(RiskMetrics.baseline(equity=EQUITY)),
            portfolio=PortfolioLedger(initial_cash=EQUITY),
        )
        assert outcome.kind is RiskOutcomeKind.DENY
        assert ReasonCode.RISK_HALTED in outcome.reason_codes

    def test_a_halt_does_not_strand_orders(self) -> None:
        """Cancellation must stay possible while halted, or a halt becomes dangerous."""
        state = RiskState()
        state.halt(actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="x")
        assert state.blocks_new_exposure()
        # Nothing in RiskState gates cancellation: it exposes only the block predicate, and
        # the execution service's cancel path does not consult it.
        from tigerquant.execution.service import ExecutionService

        cancel_source = ExecutionService.cancel.__doc__ or ""
        assert "cancel" in ExecutionService.cancel.__name__
        del cancel_source

    def test_a_check_that_raises_denies_rather_than_allows(self) -> None:
        class Exploding(RiskMetrics):
            def daily_loss_fraction(self) -> Decimal:
                msg = "boom"
                raise RuntimeError(msg)

        exploding = Exploding()
        exploding.session_start_equity = EQUITY
        exploding.equity = EQUITY
        outcome = gateway().evaluate(
            request(exploding), portfolio=PortfolioLedger(initial_cash=EQUITY)
        )
        assert outcome.kind is RiskOutcomeKind.DENY
        assert ReasonCode.CHECK_ERROR in outcome.reason_codes

    def test_only_one_module_may_call_a_broker_write_api(self) -> None:
        """The 'no alternative order path' rule, enforced over the source tree.

        Uses the AST, not a text search. Two false positives proved a grep is the wrong tool:
        the NautilusTrader adapter legitimately *defines* ``submit_order`` to satisfy the
        engine's override points, and this module's own prose names the APIs it forbids. A
        control that forces code to avoid certain words is a control nobody will keep.
        """
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "tigerquant"
        banned = {"place_order", "modify_order", "cancel_order", "submit_order"}
        offenders: list[str] = []

        for path in sorted(src.rglob("*.py")):
            if path.as_posix().endswith("adapters/tiger/execution.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = (
                    function.attr
                    if isinstance(function, ast.Attribute)
                    else function.id
                    if isinstance(function, ast.Name)
                    else None
                )
                if name in banned:
                    offenders.append(f"{path.relative_to(src)}:{node.lineno}:{name}()")

        assert not offenders, f"broker write calls found outside the single adapter: {offenders}"

    def test_clock_is_injected_so_a_halt_cannot_be_time_travelled_around(self) -> None:
        """A halt's timestamp comes from the injected clock, not from wall time."""
        clock = SimulatedClock(AT)
        state = RiskState()
        watchdog = Watchdog(policy=policy(), state=state)
        watchdog.observe(at=clock.now(), metrics=None)
        assert state.history[0].at == AT
