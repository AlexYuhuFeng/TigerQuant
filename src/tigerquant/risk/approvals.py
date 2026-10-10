"""Single-use approvals — the capability that authorises exactly one submission.

``ApprovedOrder`` is the only value the broker adapter will accept. It is minted by the
risk gateway and consumed exactly once by the execution service, so "only the risk
gateway can approve an order" and "an approved order cannot be submitted twice" are
properties of the object graph rather than of reviewer discipline (ADR-0007 §1, §7).

The token is derived deterministically from the order's identity, which means a
reconstructed replay produces the same token, and a replayed submission is rejected
because the token was already consumed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from tigerquant.core.canonical import digest_of
from tigerquant.core.enums import OrderSide, OrderType, ReasonCode, TimeInForce
from tigerquant.core.errors import RiskDenied
from tigerquant.core.ids import stable_token
from tigerquant.events.base import NonEmptyStr

if TYPE_CHECKING:
    from tigerquant.events.execution import OrderIntentCreated

__all__ = ["ApprovalRegistry", "ApprovedOrder", "OrderIntentView"]


class OrderIntentView(BaseModel):
    """The immutable content of an order intent, as approved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: NonEmptyStr
    decision_id: NonEmptyStr
    instrument: NonEmptyStr
    side: OrderSide
    quantity: int
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: Decimal | None = None
    client_order_id: NonEmptyStr

    @classmethod
    def from_event(cls, event: OrderIntentCreated) -> OrderIntentView:
        """Project a journaled ``OrderIntentCreated`` payload into a view."""
        return cls(
            intent_id=event.intent_id,
            decision_id=event.decision_id,
            instrument=event.instrument,
            side=event.side,
            quantity=event.quantity,
            order_type=event.order_type,
            time_in_force=event.time_in_force,
            limit_price=event.limit_price,
            client_order_id=event.client_order_id,
        )

    def is_buy(self) -> bool:
        return self.side is OrderSide.BUY


@dataclass(frozen=True, slots=True)
class ApprovedOrder:
    """An order the risk gateway allowed. Carries the evidence of that decision."""

    token: str
    order: OrderIntentView
    policy_hash: str
    portfolio_digest: str
    approved_at: dt.datetime
    risk_event_id: str

    @property
    def intent_id(self) -> str:
        return self.order.intent_id

    def token_material(self) -> dict[str, object]:
        return {
            "intent_id": self.order.intent_id,
            "instrument": self.order.instrument,
            "side": self.order.side,
            "quantity": self.order.quantity,
            "order_type": self.order.order_type,
            "limit_price": self.order.limit_price,
            "policy_hash": self.policy_hash,
        }


class ApprovalRegistry:
    """Holds outstanding approvals; consuming one is the only way to submit.

    The registry is deliberately in-process and non-persistent: after a restart there
    are no outstanding approvals, which is the desired fail-closed behaviour (ADR-0008
    §4 — the startup gate must reopen before new exposure).
    """

    __slots__ = ("_consumed", "_outstanding")

    def __init__(self) -> None:
        self._outstanding: dict[str, ApprovedOrder] = {}
        self._consumed: set[str] = set()

    @property
    def outstanding(self) -> tuple[str, ...]:
        return tuple(self._outstanding)

    @property
    def consumed_count(self) -> int:
        return len(self._consumed)

    def issue(
        self,
        *,
        order: OrderIntentView,
        policy_hash: str,
        portfolio_digest: str,
        approved_at: dt.datetime,
        risk_event_id: str,
    ) -> ApprovedOrder:
        """Mint a single-use approval for ``order``."""
        token = f"apr:{stable_token(digest_of({'order': order, 'policy': policy_hash}), 32)}"
        approval = ApprovedOrder(
            token=token,
            order=order,
            policy_hash=policy_hash,
            portfolio_digest=portfolio_digest,
            approved_at=approved_at,
            risk_event_id=risk_event_id,
        )
        self._outstanding[token] = approval
        return approval

    def consume(self, token: str) -> ApprovedOrder:
        """Redeem an approval exactly once.

        Raises
        ------
        RiskDenied
            If the token is unknown, already used, or was never issued by this registry
            (for example after a restart, or when an ``ApprovedOrder`` was forged).
        """
        if token in self._consumed:
            msg = (
                f"approval {token} was already consumed; refusing to submit the same "
                "intent twice (ADR-0007 §1)"
            )
            raise RiskDenied(msg, [ReasonCode.CHECK_ERROR])
        try:
            approval = self._outstanding.pop(token)
        except KeyError as exc:
            msg = (
                f"approval {token} is not outstanding; an order may only be submitted "
                "with a live risk approval from this process"
            )
            raise RiskDenied(msg, [ReasonCode.CHECK_ERROR]) from exc
        self._consumed.add(token)
        return approval

    def revoke_all(self) -> int:
        """Drop every outstanding approval. Used on halt and on shutdown."""
        count = len(self._outstanding)
        self._outstanding.clear()
        return count
