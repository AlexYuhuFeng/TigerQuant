"""Exact decimal arithmetic.

Money and prices are :class:`~decimal.Decimal` everywhere. ``float`` is rejected at the
boundary, because binary floating point cannot represent cents exactly and would make
deterministic recomputation (ADR-0006) and byte-identical replay (ADR-0011) fail.

Rounding is ``ROUND_HALF_EVEN`` (banker's rounding) at a declared scale per field type,
so the same inputs always produce the same bits.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from tigerquant.core.errors import MoneyError

__all__ = [
    "CASH_EXP",
    "PRICE_EXP",
    "WEIGHT_EXP",
    "ZERO",
    "dec_str",
    "q_cash",
    "q_price",
    "q_weight",
    "to_decimal",
    "to_quantity",
]

#: Prices quantise to 4 decimal places (US equities trade in cents, sub-penny for some
#: venues; 4 dp keeps both exact).
PRICE_EXP = Decimal("0.0001")
#: Cash, notional, commissions quantise to cents.
CASH_EXP = Decimal("0.01")
#: Portfolio weights quantise to 8 decimal places (1e-8 of equity).
WEIGHT_EXP = Decimal("0.00000001")
ZERO = Decimal(0)

_Number = str | int | Decimal


def to_decimal(value: _Number) -> Decimal:
    """Convert to :class:`Decimal`, rejecting ``float`` and non-finite values."""
    if isinstance(value, bool):
        msg = f"bool is not a monetary value: {value!r}"
        raise MoneyError(msg)
    if isinstance(value, float):
        msg = (
            f"float is not allowed for money (got {value!r}); "
            "pass a string or Decimal so the value is exact"
        )
        raise MoneyError(msg)
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        msg = f"cannot interpret {value!r} as a decimal"
        raise MoneyError(msg) from exc
    if not result.is_finite():
        msg = f"non-finite decimal is not allowed: {value!r}"
        raise MoneyError(msg)
    return result


def _quantize(value: _Number, exponent: Decimal) -> Decimal:
    return to_decimal(value).quantize(exponent, rounding=ROUND_HALF_EVEN)


def q_price(value: _Number) -> Decimal:
    """Quantise to the price scale."""
    return _quantize(value, PRICE_EXP)


def q_cash(value: _Number) -> Decimal:
    """Quantise to the cash scale."""
    return _quantize(value, CASH_EXP)


def q_weight(value: _Number) -> Decimal:
    """Quantise to the portfolio-weight scale."""
    return _quantize(value, WEIGHT_EXP)


def to_quantity(value: int) -> int:
    """Validate an integral share quantity. Fractional shares are not supported."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"quantity must be an int number of shares, got {value!r}"
        raise MoneyError(msg)
    return value


def dec_str(value: _Number) -> str:
    """Canonical string form: plain notation, no exponent, ``-0`` normalised to ``0``."""
    dec = to_decimal(value)
    if dec == ZERO:
        return "0"
    return format(dec.normalize(), "f")
