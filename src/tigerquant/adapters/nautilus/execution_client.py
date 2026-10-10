"""NautilusTrader ``ExecutionClient`` backed by our guarded broker, plus the submission gate.

This class is where the engine meets our order path, and its whole job is to be a **narrow
door**:

**The engine is never the risk authority.** An order reaching ``submit_order`` has already been
approved by :class:`~tigerquant.risk.gateway.RiskGateway` and carries a single-use
:class:`~tigerquant.risk.approvals.ApprovedOrder`. The engine may reject it further; it may
never approve something the gateway denied, and there is no path here that submits an order
without an approval (ADR-0007, ADR-0010 section 3).

**The submission target decides, not the mode.** Mode translation happens in
:mod:`~tigerquant.adapters.nautilus.modes`, and this client receives the resulting
:class:`~tigerquant.core.enums.SubmissionTarget`. When it is ``NONE`` -- backtest, shadow, and
paper-shaped rehearsal -- every submit is refused locally and recorded, so a shadow run that
"reaches a venue" is impossible rather than merely unlikely.

**There is no direct route to a vendor write.** This client wraps a
:class:`~tigerquant.execution.broker.BrokerProtocol`; it never imports a Tiger module, and the
only implementation that can touch a venue is the guarded Tiger adapter from CR-03.

Written against the source, not the documentation
-------------------------------------------------
ADR-0010 warned that the published guides describe the v2 Rust-core line and cannot be trusted
for 1.231.0. Reading the ``.pyx`` that ships inside the wheel confirmed it, twice:

* The documented constructor starts with ``loop``. In 1.231.0 there is no such parameter; the
  real signature is ``(client_id, venue, oms_type, account_type, base_currency, msgbus, cache,
  clock, config=None)`` -- eight positional arguments.
* The documented override points are ``async def _submit_order`` and friends. In 1.231.0 the
  override points are the **synchronous** ``cpdef`` methods ``submit_order(SubmitOrder)``,
  ``cancel_order(CancelOrder)`` and ``modify_order(ModifyOrder)``.

An adapter written from the guides would have been non-functional in a way that still imported
cleanly. Both facts are pinned in ``adapters/nautilus/compat.py`` so a wheel bump fails loudly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tigerquant.adapters.nautilus.compat import require_engine
from tigerquant.core.enums import (
    Mode,
    OrderSide,
    OrderType,
    ReasonCode,
    SubmissionTarget,
    TimeInForce,
)
from tigerquant.core.errors import BrokerError, RiskDenied

if TYPE_CHECKING:
    from tigerquant.execution.broker import BrokerProtocol
    from tigerquant.risk.approvals import ApprovedOrder

__all__ = [
    "EngineOrderTranslator",
    "SubmissionGate",
    "TranslationResult",
    "engine_available",
    "status_for_broker_state",
]

#: The engine's status name by our broker state. An unmapped state becomes PENDING_UPDATE
#: rather than a guess: telling the engine an order is filled when we do not know that would
#: corrupt its cache and its P&L.
_BROKER_STATE_TO_ENGINE: dict[str, str] = {
    "ACCEPTED": "SUBMITTED",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED",
    "CANCELED": "CANCELED",
    "REJECTED": "REJECTED",
    "UNKNOWN": "PENDING_UPDATE",
}


def engine_available() -> bool:
    """Whether the engine is importable. Keeps this module usable without the extra."""
    return require_engine().available


def status_for_broker_state(state: str) -> str:
    """Map our broker state onto the engine's status name."""
    return _BROKER_STATE_TO_ENGINE.get(state, "PENDING_UPDATE")


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """The outcome of translating an engine order into our internal terms."""

    ok: bool
    client_order_id: str
    instrument: str
    side: OrderSide | None
    quantity: int
    order_type: OrderType | None
    limit_price: Decimal | None = None
    time_in_force: TimeInForce | None = None
    refusal: str = ""

    @property
    def refused(self) -> bool:
        return not self.ok


@dataclass
class SubmissionGate:
    """Decides whether the engine is allowed to submit at all.

    A separate object rather than a flag, so the decision lives in one place, is testable on
    its own, and cannot be quietly bypassed by constructing a client differently.
    """

    submission_target: SubmissionTarget
    mode: Mode
    refusals: list[str] = field(default_factory=list)

    def allows_submission(self) -> bool:
        return self.submission_target is not SubmissionTarget.NONE

    def check(self, *, client_order_id: str) -> None:
        """Raise unless submission is permitted, recording the refusal either way."""
        if self.allows_submission():
            return
        reason = (
            f"submission refused for {client_order_id!r}: mode={self.mode.value} with "
            f"submission_target={self.submission_target.value}. The engine may run, but "
            "nothing leaves the process. A shadow or backtest run that reaches a venue is a "
            "bug, not a configuration choice (ADR-0010)."
        )
        self.refusals.append(reason)
        raise RiskDenied(reason, (ReasonCode.MODE_CAPABILITY_ABSENT,))


class EngineOrderTranslator:
    """Translates engine orders into our terms.

    Deliberately separate from submission, so translation can be tested exhaustively without a
    running engine node, and so an unmappable order is refused at the boundary rather than
    half-way through a submission. Every failure returns ``ok=False`` with a reason; nothing is
    guessed, because the alternative is submitting something the strategy did not ask for.
    """

    __slots__ = ()

    def translate(self, command: Any) -> TranslationResult:
        order = getattr(command, "order", None)
        if order is None:
            return _refusal("", "", "the command carries no order")

        client_order_id = str(getattr(order, "client_order_id", "") or "")
        if not client_order_id:
            return _refusal(
                "",
                "",
                "the order has no client_order_id, so it cannot be correlated back to an "
                "approved intent",
            )

        instrument_id = getattr(order, "instrument_id", None)
        instrument = str(getattr(instrument_id, "symbol", "") or instrument_id or "")
        if not instrument:
            return _refusal(client_order_id, "", "the order has no instrument id")

        raw_side = _enum_name(getattr(order, "side", None))
        side = {"BUY": OrderSide.BUY, "SELL": OrderSide.SELL}.get(raw_side)
        if side is None:
            return _refusal(
                client_order_id,
                instrument,
                f"unsupported side {raw_side!r}; this system is long-only and sends BUY or "
                "SELL only (AGENTS.md invariant 9)",
            )

        raw_type = _enum_name(getattr(order, "order_type", None))
        order_type = {"MARKET": OrderType.MARKET, "LIMIT": OrderType.LIMIT}.get(raw_type)
        if order_type is None:
            return _refusal(
                client_order_id,
                instrument,
                f"unsupported order type {raw_type!r}; this system sends market and limit "
                "orders on US equities only",
                side=side,
            )

        quantity = _quantity_of(getattr(order, "quantity", 0))
        if quantity <= 0:
            return _refusal(
                client_order_id,
                instrument,
                f"quantity must be positive, got {quantity}",
                side=side,
                order_type=order_type,
            )

        raw_tif = _enum_name(getattr(order, "time_in_force", None))
        time_in_force = {member.name: member for member in TimeInForce}.get(raw_tif)
        if time_in_force is None:
            return _refusal(
                client_order_id,
                instrument,
                f"unsupported time in force {raw_tif!r}",
                side=side,
                order_type=order_type,
                quantity=quantity,
            )

        limit_price = _price_of(getattr(order, "price", None))
        if order_type is OrderType.LIMIT and limit_price is None:
            return _refusal(
                client_order_id,
                instrument,
                "a LIMIT order requires a price",
                side=side,
                order_type=order_type,
                quantity=quantity,
            )

        return TranslationResult(
            ok=True,
            client_order_id=client_order_id,
            instrument=instrument,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
        )


def _refusal(
    client_order_id: str,
    instrument: str,
    reason: str,
    *,
    side: OrderSide | None = None,
    order_type: OrderType | None = None,
    quantity: int = 0,
) -> TranslationResult:
    return TranslationResult(
        ok=False,
        client_order_id=client_order_id,
        instrument=instrument,
        side=side,
        quantity=quantity,
        order_type=order_type,
        refusal=reason,
    )


def _enum_name(value: object) -> str:
    """The member name of an engine enum, tolerating pydantic and pyo3 wrappers alike."""
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.upper()
    return str(value or "").rsplit(".", maxsplit=1)[-1].upper()


def _quantity_of(value: object) -> int:
    as_double = getattr(value, "as_double", None)
    raw = as_double() if callable(as_double) else value
    try:
        return int(Decimal(str(raw)))
    except Exception:  # noqa: BLE001 - an unparseable quantity reads as zero and is refused
        return 0


def _price_of(value: object) -> Decimal | None:
    if value is None:
        return None
    as_double = getattr(value, "as_double", None)
    raw = as_double() if callable(as_double) else value
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None


def build_execution_client(
    *,
    broker: BrokerProtocol,
    approvals: dict[str, ApprovedOrder],
    submission_target: SubmissionTarget,
    mode: Mode,
    venue: str = "TIGER",
    client_id: str = "TIGERQUANT",
) -> Any:
    """Construct the engine ``ExecutionClient``.

    A factory rather than a class, so the engine import is confined to this function and the
    rest of the module stays importable -- and testable -- without the extra installed.

    The constructor arguments are the 1.231.0 ones, taken from the ``.pyx`` inside the wheel:
    ``(client_id, venue, oms_type, account_type, base_currency, msgbus, cache, clock)``. There
    is no ``loop`` argument in this version, whatever the published guide says.
    """
    check = require_engine()
    check.raise_if_broken()

    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.execution.client import ExecutionClient
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import (
        AccountId,
        ClientId,
        TraderId,
        Venue,
    )

    clock = LiveClock()
    msgbus = MessageBus(TraderId(f"{client_id}-001"), clock)
    cache = Cache()
    # The issuer must equal the client id: ExecutionClient._set_account_id verifies exactly
    # that, from the source in the wheel. Using the venue here raises ValueError.
    account_id = AccountId(f"{client_id}-PAPER")
    gate = SubmissionGate(submission_target=submission_target, mode=mode)
    translator = EngineOrderTranslator()

    # The engine is imported in this scope only, so mypy sees it as Any here.
    class TigerQuantExecutionClient(ExecutionClient):  # type: ignore[misc]
        """Our execution client. Every submit passes the gate before it reaches the broker."""

        def __init__(self) -> None:
            super().__init__(
                ClientId(client_id),
                Venue(venue),
                OmsType.NETTING,
                AccountType.CASH,
                USD,
                msgbus,
                cache,
                clock,
            )
            self._tq_broker = broker
            self._tq_approvals = approvals
            self._tq_gate = gate
            self._tq_translator = translator
            self._set_account_id(account_id)

        # -- our surface, prefixed so it cannot collide with the engine's ---------------

        @property
        def submission_gate(self) -> SubmissionGate:
            return self._tq_gate

        def tq_connect(self) -> None:
            """Mark the client connected. Opens no exposure in any mode."""
            self._set_connected(True)

        # -- the override points: synchronous cpdef in 1.231.0 --------------------------

        def submit_order(self, command: Any) -> None:
            order = getattr(command, "order", None)
            if order is None:
                return
            client_order_id = str(getattr(order, "client_order_id", "") or "")
            strategy_id = getattr(command, "strategy_id", None)
            instrument_id = getattr(order, "instrument_id", None)

            # Gate 1: may the engine submit at all in this configuration?
            try:
                self._tq_gate.check(client_order_id=client_order_id)
            except RiskDenied as exc:
                self.generate_order_denied(
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    reason=str(exc),
                    ts_event=clock.timestamp_ns(),
                )
                return

            # Gate 2: the order must carry a risk approval. There is no path that submits
            # without one -- the engine is not the risk authority (ADR-0007).
            approval = self._tq_approvals.get(client_order_id)
            if approval is None:
                self.generate_order_denied(
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    reason=(
                        "no risk approval exists for this client order id; an order must be "
                        "approved by the gateway before it can reach a venue"
                    ),
                    ts_event=clock.timestamp_ns(),
                )
                return

            # Gate 3: it must translate into terms our broker accepts.
            translated = self._tq_translator.translate(command)
            if translated.refused:
                self.generate_order_denied(
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    reason=translated.refusal,
                    ts_event=clock.timestamp_ns(),
                )
                return

            self.generate_order_submitted(
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                ts_event=clock.timestamp_ns(),
            )

            try:
                ack = self._tq_broker.submit(approval, at=dt.datetime.now(tz=dt.UTC))
            except BrokerError as exc:
                # An ambiguous outcome is never retried; the reconciler resolves it.
                self.generate_order_rejected(
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    reason=f"broker refused: {exc}",
                    ts_event=clock.timestamp_ns(),
                )
                return

            if status_for_broker_state(ack.state.value) == "REJECTED":
                self.generate_order_rejected(
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    reason=ack.message or "rejected by the venue",
                    ts_event=clock.timestamp_ns(),
                )
                return

            self.generate_order_accepted(
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=None,
                ts_event=clock.timestamp_ns(),
            )

        def cancel_order(self, command: Any) -> None:
            """Cancel a working order.

            Cancellation is deliberately NOT gated. A halt or a shadow run must always be able
            to reduce exposure; a risk control that can block a cancel is dangerous.
            """
            order = getattr(command, "order", None)
            if order is None:
                return
            client_order_id = str(getattr(order, "client_order_id", "") or "")
            try:
                self._tq_broker.cancel(
                    client_order_id=client_order_id, at=dt.datetime.now(tz=dt.UTC)
                )
            except BrokerError:
                return

        def modify_order(self, command: Any) -> None:
            """Refuse modification. Ignoring it silently would leave the strategy misinformed."""
            order = getattr(command, "order", None)
            if order is None:
                return
            self.generate_order_modify_rejected(
                strategy_id=getattr(command, "strategy_id", None),
                instrument_id=getattr(order, "instrument_id", None),
                client_order_id=getattr(order, "client_order_id", None),
                venue_order_id=None,
                reason="order modification is not supported by this adapter",
                ts_event=clock.timestamp_ns(),
            )

    return TigerQuantExecutionClient()
