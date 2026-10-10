"""Risk gateway: deny-by-default, fail-closed, and no bypass."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

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
from tigerquant.portfolio.ledger import PortfolioLedger
from tigerquant.risk.approvals import ApprovalRegistry, OrderIntentView
from tigerquant.risk.gateway import RiskGateway, RiskRequest
from tigerquant.risk.policy import RiskPolicy
from tigerquant.risk.state import Actor, RiskState

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
MARKS = {"SPY": Decimal("600.00"), "QQQ": Decimal("500.00")}


def make_order(
    *,
    instrument: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 8,
    intent_id: str = "bt-oi_test",
) -> OrderIntentView:
    return OrderIntentView(
        intent_id=intent_id,
        decision_id="bt-dec_test",
        instrument=instrument,
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        client_order_id="TQ-bt-test",
    )


def make_request(order: OrderIntentView, **overrides: object) -> RiskRequest:
    base: dict[str, object] = {
        "order": order,
        "marks": dict(MARKS),
        "mark_available_at": {"SPY": AT, "QQQ": AT},
        "as_of": AT,
        "startup_gate_open": True,
        "orders_this_cycle": 0,
        "snapshot_usable": True,
        "session": TradingSession(),
        "submission_target": SubmissionTarget.SIMULATION,
        "mode": Mode.BACKTEST,
    }
    base.update(overrides)
    return RiskRequest(**base)  # type: ignore[arg-type]


def deny_codes(outcome: object) -> set[ReasonCode]:
    return set(outcome.reason_codes)  # type: ignore[attr-defined]


class TestDenyByDefault:
    def test_a_clean_order_is_allowed(self, gateway: RiskGateway, ledger: PortfolioLedger) -> None:
        outcome = gateway.evaluate(make_request(make_order()), portfolio=ledger)
        assert outcome.kind is RiskOutcomeKind.ALLOW
        assert outcome.reason_codes == ()
        assert outcome.policy_hash.startswith("sha256:")

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"startup_gate_open": False}, ReasonCode.STARTUP_GATE_CLOSED),
            ({"snapshot_usable": False}, ReasonCode.SNAPSHOT_INCOMPLETE),
            ({"orders_this_cycle": 1}, ReasonCode.ORDER_RATE_EXCEEDED),
            ({"blocking_intents": 1}, ReasonCode.UNRECONCILED),
            ({"submission_target": SubmissionTarget.NONE}, ReasonCode.MODE_CAPABILITY_ABSENT),
            ({"submission_target": SubmissionTarget.BROKER}, ReasonCode.MODE_CAPABILITY_ABSENT),
            ({"session": None}, ReasonCode.MARKET_CLOSED),
        ],
    )
    def test_each_gate_denies(
        self,
        gateway: RiskGateway,
        ledger: PortfolioLedger,
        overrides: dict[str, object],
        expected: ReasonCode,
    ) -> None:
        outcome = gateway.evaluate(make_request(make_order(), **overrides), portfolio=ledger)
        assert outcome.kind is RiskOutcomeKind.DENY
        assert expected in deny_codes(outcome)

    def test_broker_target_is_allowed_in_paper_mode(
        self, policy: RiskPolicy, risk_state: RiskState, ledger: PortfolioLedger
    ) -> None:
        gateway = RiskGateway(policy=policy, state=risk_state, registry=ApprovalRegistry())
        request = make_request(
            make_order(), submission_target=SubmissionTarget.BROKER, mode=Mode.PAPER
        )
        assert gateway.evaluate(request, portfolio=ledger).allowed

    def test_paper_mode_may_not_use_a_simulator(
        self, policy: RiskPolicy, risk_state: RiskState, ledger: PortfolioLedger
    ) -> None:
        """A 'paper' run backed by a simulator would produce falsely-labelled results."""
        gateway = RiskGateway(policy=policy, state=risk_state, registry=ApprovalRegistry())
        request = make_request(
            make_order(), submission_target=SubmissionTarget.SIMULATION, mode=Mode.PAPER
        )
        outcome = gateway.evaluate(request, portfolio=ledger)
        assert outcome.kind is RiskOutcomeKind.DENY
        assert ReasonCode.MODE_CAPABILITY_ABSENT in deny_codes(outcome)


class TestLimitChecks:
    def test_symbol_outside_the_universe_is_denied(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        outcome = gateway.evaluate(make_request(make_order(instrument="TSLA")), portfolio=ledger)
        assert ReasonCode.SYMBOL_NOT_ALLOWED in deny_codes(outcome)

    def test_order_larger_than_the_per_order_cap_is_denied(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        # 0.10 * 100_000 = 10_000 allowed; 40 shares * 600 = 24_000 is far above it.
        outcome = gateway.evaluate(make_request(make_order(quantity=40)), portfolio=ledger)
        assert ReasonCode.MAX_ORDER_FRACTION_EXCEEDED in deny_codes(outcome)

    def test_symbol_concentration_cap_is_denied(
        self, policy: RiskPolicy, risk_state: RiskState, ledger: PortfolioLedger
    ) -> None:
        tight = policy.model_copy(update={"max_symbol_fraction": Decimal("0.01")})
        gateway = RiskGateway(policy=tight, state=risk_state, registry=ApprovalRegistry())
        outcome = gateway.evaluate(make_request(make_order(quantity=2)), portfolio=ledger)
        assert ReasonCode.MAX_SYMBOL_FRACTION_EXCEEDED in deny_codes(outcome)

    def test_gross_exposure_cap_is_denied(
        self, policy: RiskPolicy, risk_state: RiskState, ledger: PortfolioLedger
    ) -> None:
        tight = policy.model_copy(
            update={"gross_exposure_fraction": Decimal("0.001"), "max_symbol_fraction": Decimal(1)}
        )
        gateway = RiskGateway(policy=tight, state=risk_state, registry=ApprovalRegistry())
        outcome = gateway.evaluate(make_request(make_order(quantity=2)), portfolio=ledger)
        assert ReasonCode.GROSS_EXPOSURE_EXCEEDED in deny_codes(outcome)

    def test_insufficient_cash_is_denied(self, gateway: RiskGateway) -> None:
        poor = PortfolioLedger(initial_cash=Decimal("1000.00"))
        outcome = gateway.evaluate(make_request(make_order(quantity=5)), portfolio=poor)
        assert ReasonCode.INSUFFICIENT_BUYING_POWER in deny_codes(outcome)

    def test_stale_mark_is_denied(self, gateway: RiskGateway, ledger: PortfolioLedger) -> None:
        request = make_request(
            make_order(), mark_available_at={"SPY": AT - dt.timedelta(hours=1), "QQQ": AT}
        )
        outcome = gateway.evaluate(request, portfolio=ledger)
        assert ReasonCode.MARK_STALE in deny_codes(outcome)

    def test_future_dated_mark_is_denied_as_clock_skew(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        request = make_request(
            make_order(), mark_available_at={"SPY": AT + dt.timedelta(minutes=5), "QQQ": AT}
        )
        outcome = gateway.evaluate(request, portfolio=ledger)
        assert ReasonCode.MARK_STALE in deny_codes(outcome)

    def test_missing_mark_is_denied(self, gateway: RiskGateway, ledger: PortfolioLedger) -> None:
        request = make_request(
            make_order(), marks={"QQQ": Decimal(500)}, mark_available_at={"QQQ": AT}
        )
        outcome = gateway.evaluate(request, portfolio=ledger)
        assert ReasonCode.MARK_MISSING in deny_codes(outcome)

    def test_zero_quantity_is_denied(self, gateway: RiskGateway, ledger: PortfolioLedger) -> None:
        outcome = gateway.evaluate(make_request(make_order(quantity=0)), portfolio=ledger)
        assert ReasonCode.ZERO_QUANTITY in deny_codes(outcome)

    def test_selling_without_a_position_is_denied(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        outcome = gateway.evaluate(
            make_request(make_order(side=OrderSide.SELL, quantity=5)), portfolio=ledger
        )
        assert ReasonCode.SHORT_NOT_ALLOWED in deny_codes(outcome)

    def test_market_closed_is_denied(self, gateway: RiskGateway, ledger: PortfolioLedger) -> None:
        closed = make_request(make_order(), as_of=dt.datetime(2026, 3, 2, 22, 0, tzinfo=dt.UTC))
        outcome = gateway.evaluate(closed, portfolio=ledger)
        assert ReasonCode.MARKET_CLOSED in deny_codes(outcome)

    def test_halted_instrument_is_denied(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        request = make_request(make_order(), session=TradingSession(halted_instruments=("SPY",)))
        outcome = gateway.evaluate(request, portfolio=ledger)
        assert ReasonCode.INSTRUMENT_HALTED in deny_codes(outcome)

    def test_every_check_is_recorded(self, gateway: RiskGateway, ledger: PortfolioLedger) -> None:
        outcome = gateway.evaluate(make_request(make_order()), portfolio=ledger)
        assert "no_blocking_intents" in outcome.checks_run
        assert len(outcome.checks_run) >= 15


class TestFailClosed:
    def test_risk_state_halt_denies(
        self, gateway: RiskGateway, risk_state: RiskState, ledger: PortfolioLedger
    ) -> None:
        risk_state.halt(
            actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="test"
        )
        outcome = gateway.evaluate(make_request(make_order()), portfolio=ledger)
        assert ReasonCode.RISK_HALTED in deny_codes(outcome)

    def test_an_exception_inside_evaluation_becomes_a_denial(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        class Exploding(PortfolioLedger):
            def equity(self, marks: dict[str, Decimal]) -> Decimal:
                raise RuntimeError("boom")

        outcome = gateway.evaluate(
            make_request(make_order()), portfolio=Exploding(initial_cash=Decimal(1))
        )
        assert outcome.kind is RiskOutcomeKind.DENY
        assert ReasonCode.CHECK_ERROR in deny_codes(outcome)

    def test_duplicate_reason_codes_are_deduplicated(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        outcome = gateway.evaluate(
            make_request(make_order(instrument="TSLA", quantity=0)), portfolio=ledger
        )
        assert len(outcome.reason_codes) == len(set(outcome.reason_codes))


class TestApprovals:
    def test_approval_is_single_use(
        self, gateway: RiskGateway, registry: ApprovalRegistry, ledger: PortfolioLedger
    ) -> None:
        outcome = gateway.evaluate(make_request(make_order()), portfolio=ledger)
        approval = gateway.issue(
            outcome,
            order=make_order(),
            portfolio_digest=outcome.portfolio_digest,
            approved_at=AT,
            risk_event_id="ev-1",
        )
        assert registry.consume(approval.token).intent_id == "bt-oi_test"
        with pytest.raises(RiskDenied, match="already consumed"):
            registry.consume(approval.token)

    def test_unknown_token_is_refused(self, registry: ApprovalRegistry) -> None:
        with pytest.raises(RiskDenied, match="not outstanding"):
            registry.consume("apr:forged")

    def test_denied_outcome_cannot_be_issued(
        self, gateway: RiskGateway, ledger: PortfolioLedger
    ) -> None:
        outcome = gateway.evaluate(make_request(make_order(instrument="TSLA")), portfolio=ledger)
        with pytest.raises(RiskDenied):
            gateway.issue(
                outcome,
                order=make_order(),
                portfolio_digest="x",
                approved_at=AT,
                risk_event_id="ev-1",
            )


class TestRiskStateControl:
    def test_agent_may_not_halt(self, risk_state: RiskState) -> None:
        with pytest.raises(PermissionError):
            risk_state.halt(
                actor=Actor.AGENT, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="x"
            )

    def test_agent_may_not_clear(self, risk_state: RiskState) -> None:
        risk_state.halt(actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="x")
        with pytest.raises(PermissionError):
            risk_state.clear(actor=Actor.AGENT, at=AT)

    def test_watchdog_may_not_clear(self, risk_state: RiskState) -> None:
        risk_state.halt(actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="x")
        with pytest.raises(PermissionError):
            risk_state.clear(actor=Actor.WATCHDOG, at=AT)

    def test_owner_may_clear(self, risk_state: RiskState) -> None:
        risk_state.halt(actor=Actor.WATCHDOG, at=AT, reason_code=ReasonCode.RISK_HALTED, detail="x")
        assert risk_state.clear(actor=Actor.OWNER, at=AT)
        assert not risk_state.blocks_new_exposure()

    def test_policy_cannot_be_mutated(self, policy: RiskPolicy) -> None:
        with pytest.raises(Exception, match="frozen|Instance is frozen"):
            policy.max_order_fraction = Decimal(1)  # type: ignore[misc]

    def test_policy_hash_changes_with_content(self, policy: RiskPolicy) -> None:
        other = policy.model_copy(update={"max_order_fraction": Decimal("0.2")})
        assert policy.content_hash() != other.content_hash()
