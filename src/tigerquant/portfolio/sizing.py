"""Deterministic position sizing.

The model never states a share quantity, a notional, a cost or buying power
(invariant 5, ADR-0006 §2). It states at most a target *weight*; everything numeric is
recomputed here and in :mod:`tigerquant.portfolio.ledger`.

Rounding is always toward zero shares: rounding a target *up* could silently exceed the
weight the decision asked for and the order-size limit that guards it.
"""

from __future__ import annotations

from decimal import Decimal

from tigerquant.core.errors import MoneyError
from tigerquant.core.money import ZERO, q_cash, q_price, q_weight, to_decimal

__all__ = ["implied_weight", "notional", "order_quantity", "shares_for_weight", "target_notional"]


def _positive(value: Decimal | str, name: str) -> Decimal:
    dec = to_decimal(value)
    if dec <= ZERO:
        msg = f"{name} must be positive, got {dec}"
        raise MoneyError(msg)
    return dec


def shares_for_weight(
    *, target_weight: Decimal | str, equity: Decimal | str, price: Decimal | str
) -> int:
    """Whole shares whose value is closest to (but not above) ``target_weight`` of equity."""
    weight = to_decimal(target_weight)
    if weight < ZERO or weight > Decimal(1):
        msg = f"target_weight must be within [0, 1], got {weight}"
        raise MoneyError(msg)
    equity_dec = _positive(equity, "equity")
    price_dec = q_price(_positive(price, "price"))
    if weight == ZERO:
        return 0
    return int((equity_dec * weight / price_dec).to_integral_value(rounding="ROUND_FLOOR"))


def order_quantity(*, target_shares: int, current_shares: int) -> int:
    """Signed share delta needed to move from ``current_shares`` to ``target_shares``."""
    if isinstance(target_shares, bool) or not isinstance(target_shares, int):
        msg = f"target_shares must be an int, got {target_shares!r}"
        raise MoneyError(msg)
    if isinstance(current_shares, bool) or not isinstance(current_shares, int):
        msg = f"current_shares must be an int, got {current_shares!r}"
        raise MoneyError(msg)
    return target_shares - current_shares


def notional(*, quantity: int, price: Decimal | str) -> Decimal:
    """Absolute notional of ``quantity`` shares at ``price``."""
    price_dec = q_price(_positive(price, "price"))
    return q_cash(abs(quantity) * price_dec)


def target_notional(*, target_weight: Decimal | str, equity: Decimal | str) -> Decimal:
    """Dollar value implied by a target weight."""
    weight = to_decimal(target_weight)
    if weight < ZERO or weight > Decimal(1):
        msg = f"target_weight must be within [0, 1], got {weight}"
        raise MoneyError(msg)
    return q_cash(_positive(equity, "equity") * weight)


def implied_weight(*, quantity: int, price: Decimal | str, equity: Decimal | str) -> Decimal:
    """Portfolio weight implied by holding ``quantity`` shares at ``price``."""
    equity_dec = _positive(equity, "equity")
    return q_weight(notional(quantity=quantity, price=price) / equity_dec)
