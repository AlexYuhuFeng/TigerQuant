"""Tiger PAPER execution adapter — the only broker write path in this system.

Everything about this module is shaped by one requirement: an order must be impossible to
place by accident, by a bug elsewhere, or by a hostile input. Four independent gates stand
between an intent and a real submitted order, and all four must pass:

1. **A write-scoped credential.** The adapter refuses to construct at all unless the OAuth
   grant includes ``api.trade:write``. Development holds a read-only grant on purpose, so
   the default state of this repository is "cannot submit" rather than "submits carefully".
2. **A verified paper account.** The account guard must have confirmed, from the broker's own
   response, that the target is a PAPER account (ADR-0003 §3).
3. **A single-use risk approval.** Only
   :class:`~tigerquant.risk.approvals.ApprovedOrder` is accepted, and the registry consumes
   the token exactly once, so an intent cannot be submitted twice even if the caller tries.
4. **An account match at the last line before the network.** ``place_order`` sends
   ``order.account``, *not* ``config.account``, and the SDK lets an ``Order`` built for one
   account be submitted through a client configured for another. The order object is built
   with the **verified** account and asserted immediately before the call.

Two behaviours are deliberately absent:

* **No retry, ever.** ``SKIP_RETRY_SERVICES`` in the SDK already excludes order mutations
  from its backoff logic, and this adapter adds no retry of its own. A timeout after
  transmission becomes :attr:`~tigerquant.execution.broker.BrokerOrderState.UNKNOWN`, and
  the only way out is reconciliation (ADR-0008 §3).
* **No client-side idempotency claim.** Tiger has no documented client-order-id field with
  idempotency semantics, so ``client_order_id`` is passed as ``external_id`` purely as a
  correlation hint and is never relied upon to prevent a duplicate. Duplicate prevention
  rests on our own approval registry and reconciler.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tigerquant.adapters.tiger.guard import mask_account
from tigerquant.core.enums import (
    ImplementationStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    ReasonCode,
)
from tigerquant.core.errors import BrokerError, ConfigurationError
from tigerquant.core.money import dec_str, to_decimal
from tigerquant.execution.broker import (
    BrokerAck,
    BrokerCancel,
    BrokerFill,
    BrokerOrderState,
    BrokerOrderView,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tigerquant.adapters.tiger.guard import TigerAccountGuard
    from tigerquant.adapters.tiger.oauth import TigerQuantOAuthSession
    from tigerquant.core.clock import Clock
    from tigerquant.risk.approvals import ApprovedOrder

__all__ = [
    "WRITE_SCOPE",
    "TigerPaperExecutionClient",
    "require_write_scope",
]

#: The scope an execution adapter must hold. Development holds a read-only grant.
WRITE_SCOPE = "api.trade:write"

#: Tiger's order types, restricted to what this system may send. Long-only, no margin, no
#: options means market and limit orders on US equities and nothing else.
_SUPPORTED_ORDER_TYPES: dict[OrderType, str] = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT: "LMT",
}

#: Tiger status strings mapped onto the broker-transport vocabulary. An unmapped status is
#: surfaced verbatim rather than guessed at.
_STATUS_TO_STATE: dict[str, BrokerOrderState] = {
    "PendingNew": BrokerOrderState.ACCEPTED,
    "Initial": BrokerOrderState.ACCEPTED,
    "Submitted": BrokerOrderState.ACCEPTED,
    "PendingCancel": BrokerOrderState.ACCEPTED,
    "PartiallyFilled": BrokerOrderState.PARTIALLY_FILLED,
    "Filled": BrokerOrderState.FILLED,
    "Cancelled": BrokerOrderState.CANCELED,
    "Inactive": BrokerOrderState.REJECTED,
    "Invalid": BrokerOrderState.REJECTED,
}

#: Once an order reaches one of these, it is finished at the venue.
_TERMINAL_TIGER_STATUS = frozenset({"Filled", "Cancelled", "Inactive", "Invalid"})


def require_write_scope(session: TigerQuantOAuthSession) -> None:
    """Refuse to build an execution client on a credential that cannot trade.

    This is the first of the four gates, and the one that makes the repository's default
    state safe: development holds a read-only grant, so importing the adapter is fine but
    constructing it fails loudly with an explanation rather than at the first order.

    Re-authorization is an owner action performed in a browser. No code path here can
    obtain a wider grant on its own.
    """
    granted = set(session.scopes.split())
    if WRITE_SCOPE not in granted:
        msg = (
            f"this credential does not hold {WRITE_SCOPE!r}; it holds "
            f"{sorted(granted)}. Order submission is therefore impossible, which is the "
            "intended default. Placing orders requires the owner to re-authorize with a "
            "write-scoped grant — a deliberate, separate decision (ADR-0012)."
        )
        raise ConfigurationError(msg)


class TigerPaperExecutionClient:
    """Guarded PAPER execution. Implements :class:`~tigerquant.execution.broker.BrokerProtocol`.

    Constructing this is the only way to reach a Tiger write API anywhere in the codebase;
    a source-tree scan in the test suite asserts that no other module names one.
    """

    __slots__ = (
        "_client",
        "_clock",
        "_guard",
        "_session",
        "_submitted",
        "_tiger_client_id",
        "_venue_confirmed_an_order",
    )

    def __init__(
        self,
        *,
        session: TigerQuantOAuthSession,
        trade_client: Any,
        guard: TigerAccountGuard,
        clock: Clock,
    ) -> None:
        # Gate 1: the credential must be able to trade at all.
        require_write_scope(session)
        # Gate 2: a paper account must have been verified from the broker's own response.
        if not guard.is_verified:
            msg = (
                "the account guard has not verified a PAPER account; refusing to construct "
                "an execution client. Run the guard first (ADR-0003 s3)."
            )
            raise ConfigurationError(msg)
        self._session = session
        self._client = trade_client
        self._guard = guard
        #: Injected time. A broker fill's timestamp must come from the clock like every other
        #: timestamp in the system, or a replay of the same broker state would produce a
        #: different journal (ADR-0011).
        self._clock = clock
        #: Broker order id -> our client order id, for reconciliation. Tiger has no
        #: idempotency key, so this map is how we correlate a venue order back to an intent.
        self._tiger_client_id: dict[str, str] = {}
        self._submitted: dict[str, str] = {}
        #: Set only when the venue itself returned an order id. Never set by configuration,
        #: so the implementation status cannot be inflated by a hopeful setting.
        self._venue_confirmed_an_order = False

    # -- metadata ----------------------------------------------------------------

    @property
    def implementation_status(self) -> ImplementationStatus:
        """``SIMULATED`` until a real order has actually reached the paper venue.

        Holding a write scope and a verified account is a *precondition* for paper trading,
        not evidence of it. This adapter has never submitted anything: development holds a
        read-only grant, so every test of it runs against a fake SDK. Reporting
        ``PAPER_VERIFIED`` here because the configuration *looks* right would be exactly the
        placeholder-that-appears-production-ready that AGENTS.md invariant 11 forbids.

        The status becomes ``PAPER_VERIFIED`` only once the venue itself has returned an
        order id — observed evidence, not intent. It can never become ``LIVE_VERIFIED``:
        that would require a funded account this project does not use and must not use.
        """
        return (
            ImplementationStatus.PAPER_VERIFIED
            if self._venue_confirmed_an_order
            else ImplementationStatus.SIMULATED
        )

    @property
    def masked_account(self) -> str:
        return self._guard.require_verified().masked()

    def submitted_client_order_ids(self) -> tuple[str, ...]:
        """Our client order ids, in submission order. For the audit record."""
        return tuple(self._submitted)

    # -- BrokerProtocol: submit --------------------------------------------------

    def submit(self, approval: ApprovedOrder, *, at: dt.datetime) -> BrokerAck:
        """Submit an approved order, exactly once, or report that the outcome is unknown.

        The order object is built from the **verified** account and asserted immediately
        before the call, closing the documented ``order.account != config.account`` hazard.
        """
        order_view = approval.order
        client_order_id = order_view.client_order_id

        if client_order_id in self._submitted:
            # The approval registry already makes this impossible; this is the second lock
            # on the same door, because a duplicate order is unrecoverable.
            msg = (
                f"client order id {client_order_id!r} has already been submitted by this "
                "client; refusing to submit it again"
            )
            raise BrokerError(msg)

        # Three checks immediately before the network, in increasing order of specificity, and
        # all of them here rather than at construction, because construction happened once and
        # this happens every time:
        #
        #   1. the credential still holds api.trade:write. A token can be rotated or re-scoped
        #      under a running process, and the scope is re-read rather than trusted from the
        #      session that was handed to __init__.
        #   2. the broker still reports this account as PAPER. A single OAuth grant was observed
        #      returning BOTH a live-capable and a paper account (docs/evidence/
        #      cr02_authorization.txt), so "which account will this order hit" is a question
        #      that has to be re-asked, not remembered.
        #   3. the order object carries that same account. `place_order` sends order.account,
        #      NOT config.account, so if the object disagrees nothing else will notice.
        require_write_scope(self._session)
        identity = self._guard.reverify_from_client(self._client)
        self._guard.assert_verified_is_paper()

        tiger_order = self._build_order(order_view, account=identity.account)
        self._guard.assert_account_matches(tiger_order.account)

        self._submitted[client_order_id] = identity.account

        try:
            order_id = self._client.place_order(tiger_order)
        except Exception as exc:  # noqa: BLE001 - the whole point is ambiguity
            # A timeout or transport failure after transmission is NOT a failure to submit.
            # The SDK does not retry order mutations, and neither do we: the outcome is
            # unknown, and reconciliation is the only way to resolve it (ADR-0008 s3).
            return BrokerAck(
                client_order_id=client_order_id,
                state=BrokerOrderState.UNKNOWN,
                broker_order_id=None,
                remaining=order_view.quantity,
                message=(
                    f"submission outcome unknown: {type(exc).__name__}: {exc}. "
                    "No retry will be attempted; reconciliation must resolve this."
                ),
            )

        broker_order_id = str(
            order_id
            or getattr(tiger_order, "order_id", None)
            or getattr(tiger_order, "id", "")
            or ""
        )
        if not broker_order_id:
            return BrokerAck(
                client_order_id=client_order_id,
                state=BrokerOrderState.UNKNOWN,
                remaining=order_view.quantity,
                message="the venue returned no order id; outcome unknown, not retried",
            )
        self._tiger_client_id[broker_order_id] = client_order_id
        # The venue answered with a real order id: this is the observation that upgrades the
        # implementation status from SIMULATED to PAPER_VERIFIED.
        self._venue_confirmed_an_order = True

        view = self.query(client_order_id=client_order_id)
        if view is None:
            # Accepted but not yet readable. Still an acknowledgement, not a rejection.
            return BrokerAck(
                client_order_id=client_order_id,
                state=BrokerOrderState.ACCEPTED,
                broker_order_id=broker_order_id,
                remaining=order_view.quantity,
                message="accepted; the venue has not yet reported a status",
            )
        return BrokerAck(
            client_order_id=client_order_id,
            state=view.state,
            broker_order_id=view.broker_order_id,
            fills=self._fills_since(view),
            remaining=view.remaining,
            message=f"venue status {view.state.value}",
        )

    def _build_order(self, order_view: Any, *, account: str) -> Any:
        from tigeropen.common.consts import OrderType as TigerOrderType  # noqa: F401
        from tigeropen.common.util.contract_utils import stock_contract
        from tigeropen.trade.domain.order import Order

        if order_view.order_type not in _SUPPORTED_ORDER_TYPES:
            msg = (
                f"order type {order_view.order_type!r} is not supported; this system sends "
                "market and limit orders on US equities only (AGENTS.md invariant 9)"
            )
            raise BrokerError(msg)
        if order_view.order_type is OrderType.LIMIT and order_view.limit_price is None:
            msg = "a LIMIT order requires a limit price"
            raise BrokerError(msg)
        if order_view.side not in {OrderSide.BUY, OrderSide.SELL}:
            msg = f"unsupported side {order_view.side!r}"
            raise BrokerError(msg)
        if order_view.quantity <= 0:
            msg = f"quantity must be positive, got {order_view.quantity}"
            raise BrokerError(msg)

        contract = stock_contract(symbol=order_view.instrument, currency="USD")
        return Order(
            # The VERIFIED account, never the configured one.
            account=account,
            contract=contract,
            action="BUY" if order_view.side is OrderSide.BUY else "SELL",
            order_type=_SUPPORTED_ORDER_TYPES[order_view.order_type],
            quantity=order_view.quantity,
            limit_price=None
            if order_view.limit_price is None
            else float(dec_str(order_view.limit_price)),
            time_in_force=order_view.time_in_force.value,
            # A correlation hint only. Tiger documents no idempotency semantics for this
            # field, so it is never relied on to prevent a duplicate order.
            external_id=order_view.client_order_id,
        )

    # -- BrokerProtocol: cancel --------------------------------------------------

    def cancel(self, *, client_order_id: str, at: dt.datetime) -> BrokerCancel:
        """Cancel a working order. Ambiguity is reported, never retried."""
        del at
        broker_order_id = self._broker_order_id_for(client_order_id)
        if broker_order_id is None:
            return BrokerCancel(
                client_order_id=client_order_id,
                state=BrokerOrderState.UNKNOWN,
                canceled_quantity=0,
                message="no venue order id is known for this client order id",
            )
        before = self.query(client_order_id=client_order_id)
        try:
            self._client.cancel_order(
                account=self._guard.require_verified().account, order_id=broker_order_id
            )
        except Exception as exc:  # noqa: BLE001 - ambiguity is the expected failure mode
            return BrokerCancel(
                client_order_id=client_order_id,
                state=BrokerOrderState.UNKNOWN,
                canceled_quantity=0,
                message=(
                    f"cancel outcome unknown: {type(exc).__name__}: {exc}. "
                    "Not retried; reconciliation must resolve this."
                ),
            )
        after = self.query(client_order_id=client_order_id)
        remaining = before.remaining if before is not None else 0
        state = after.state if after is not None else BrokerOrderState.CANCELED
        return BrokerCancel(
            client_order_id=client_order_id,
            state=state,
            canceled_quantity=remaining if state is BrokerOrderState.CANCELED else 0,
            message=f"venue status {state.value}",
        )

    # -- BrokerProtocol: query ---------------------------------------------------

    def query(self, *, client_order_id: str) -> BrokerOrderView | None:
        """Look an order up by our client order id, using ``external_id`` where possible."""
        broker_order_id = self._broker_order_id_for(client_order_id)
        account = self._guard.require_verified().account
        try:
            order = (
                self._client.get_order(account=account, order_id=broker_order_id)
                if broker_order_id
                else None
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable order is "not found"
            del exc
            return None
        if order is None:
            return None
        return self._to_view(order)

    def poll(self, *, at: dt.datetime) -> tuple[BrokerFill, ...]:
        """Fills that appeared without a request. Returns them for the reconciler to book."""
        del at
        # Polling is expressed through query() by the execution service's reconcile pass, so
        # there is nothing to drain here. Returning an empty tuple is honest: this adapter
        # has no push subscriber in CR-03, and push integration is CR-09 work.
        return ()

    # -- mapping -----------------------------------------------------------------

    def _broker_order_id_for(self, client_order_id: str) -> str | None:
        for broker_order_id, mapped in self._tiger_client_id.items():
            if mapped == client_order_id:
                return broker_order_id
        return None

    def _to_view(self, order: Any) -> BrokerOrderView:
        raw_status = str(getattr(order, "status", "") or "")
        state = _STATUS_TO_STATE.get(raw_status, BrokerOrderState.UNKNOWN)
        quantity = _as_int(getattr(order, "quantity", 0))
        filled = _as_int(getattr(order, "filled", 0))
        external = str(getattr(order, "external_id", "") or "")
        broker_order_id = str(getattr(order, "order_id", None) or getattr(order, "id", "") or "")
        if external and broker_order_id:
            self._tiger_client_id.setdefault(broker_order_id, external)
        fill = self._fill_from_order(order, filled)
        return BrokerOrderView(
            client_order_id=external or self._tiger_client_id.get(broker_order_id, ""),
            broker_order_id=broker_order_id,
            state=state,
            filled_quantity=filled,
            remaining=max(quantity - filled, 0),
            fills=() if fill is None else (fill,),
        )

    def _fill_from_order(self, order: Any, filled: int) -> BrokerFill | None:
        """Derive a fill from an order that reports a positive filled quantity.

        Tiger's ``get_order`` reports cumulative filled quantity and average price rather than
        individual executions, so a fill derived here uses a synthetic execution id built
        from the order id and the cumulative quantity. That id is stable for a given
        cumulative state, which is what makes booking idempotent: re-reading the same order
        produces the same ``exec_id`` and the ledger books it once (acceptance gate 7).

        Individual executions would give exact per-fill pricing. That needs ``get_transactions``
        (prime accounts only, per the SDK documentation) or a push subscription, both of
        which are CR-09 work; until then a partially filled order is booked at its average
        price, which is exact for the cumulative position even if it is not per-execution.
        """
        if filled <= 0:
            return None
        broker_order_id = str(getattr(order, "order_id", None) or getattr(order, "id", "") or "")
        average = to_decimal(str(getattr(order, "avg_fill_price", 0) or 0))
        if average <= 0:
            return None
        return BrokerFill(
            exec_id=f"tiger:{broker_order_id}:{filled}",
            quantity=filled,
            price=average.quantize(Decimal("0.0001")),
            commission=to_decimal(str(getattr(order, "commission", 0) or 0)).quantize(
                Decimal("0.01")
            ),
            occurred_at=self._clock.now(),
            broker_order_id=broker_order_id,
        )

    def _fills_since(self, view: BrokerOrderView) -> tuple[BrokerFill, ...]:
        return view.fills

    # -- reporting ---------------------------------------------------------------

    def describe(self) -> dict[str, str]:
        """Audit-safe description. Account masked; no token, no order ids."""
        identity = self._guard.require_verified()
        return {
            "implementation_status": self.implementation_status.value,
            "venue_confirmed_an_order": str(self._venue_confirmed_an_order),
            "account": mask_account(identity.account),
            "scopes": " ".join(sorted(self._session.scopes.split())),
            "submitted": str(len(self._submitted)),
        }


def _as_int(value: object) -> int:
    parsed = to_decimal(str(value or 0))
    return int(parsed)


def map_status(raw: str) -> OrderStatus | None:
    """Map a Tiger status string onto our own state machine label, or ``None``.

    Exposed separately so a test can assert that every status the SDK can return is either
    mapped or deliberately unmapped, rather than silently falling through to a wrong state.
    """
    return {
        "PendingNew": OrderStatus.SUBMITTING,
        "Initial": OrderStatus.ACKNOWLEDGED,
        "Submitted": OrderStatus.ACKNOWLEDGED,
        "PendingCancel": OrderStatus.CANCEL_PENDING,
        "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
        "Filled": OrderStatus.FILLED,
        "Cancelled": OrderStatus.CANCELED,
        "Inactive": OrderStatus.REJECTED,
        "Invalid": OrderStatus.REJECTED,
    }.get(raw)


def supported_order_types() -> Sequence[str]:
    """The Tiger order-type codes this adapter will send."""
    return tuple(_SUPPORTED_ORDER_TYPES.values())


def reason_for_state(state: BrokerOrderState) -> ReasonCode:
    """The reason code to journal for a broker outcome."""
    return {
        BrokerOrderState.UNKNOWN: ReasonCode.SUBMIT_TIMEOUT,
        BrokerOrderState.REJECTED: ReasonCode.BROKER_REJECTED,
        BrokerOrderState.CANCELED: ReasonCode.RISK_HALTED,
    }.get(state, ReasonCode.CHECK_ERROR)
