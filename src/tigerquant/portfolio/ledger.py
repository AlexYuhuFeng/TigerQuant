"""Portfolio ledger — deterministic accounting projection.

The ledger is rebuilt by folding fill events (ADR-0004 §6). It is the *only* source of
position size, cash, cost basis and P&L; the model never supplies these (invariant 5).

Conventions
-----------
* Cash and cost basis are :class:`~decimal.Decimal`; there is no float arithmetic.
* Commission is capitalised into the cost basis on a buy and deducted from realized
  P&L on a sell.
* Fills are idempotent by ``exec_id``: replaying the same fill books nothing.
* A sell larger than the position is rejected unless shorting is explicitly enabled,
  which no supported mode does (invariant 9).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from tigerquant.core.canonical import digest_of
from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import OrderSide
from tigerquant.core.errors import MoneyError
from tigerquant.core.money import ZERO, q_cash, q_price, to_decimal, to_quantity

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["FillResult", "PortfolioLedger", "Position"]


@dataclass(frozen=True, slots=True)
class Position:
    """A holding. ``quantity`` is integral shares; negative means short (never allowed)."""

    instrument: str
    quantity: int
    average_cost: Decimal
    realized_pnl: Decimal

    def market_value(self, mark: Decimal) -> Decimal:
        return q_cash(to_decimal(mark) * self.quantity)

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        return q_cash((to_decimal(mark) - self.average_cost) * self.quantity)

    def is_flat(self) -> bool:
        return self.quantity == 0


@dataclass(frozen=True, slots=True)
class FillResult:
    """What a fill did, or ``None`` from :meth:`PortfolioLedger.apply_fill` if duplicate."""

    instrument: str
    position: Position
    cash: Decimal
    realized_delta: Decimal


class PortfolioLedger:
    """Cash and positions, updated only by :meth:`apply_fill`."""

    __slots__ = ("_allow_short", "_applied_exec_ids", "_cash", "_initial_cash", "_positions")

    def __init__(self, *, initial_cash: Decimal | str, allow_short: bool = False) -> None:
        cash = q_cash(to_decimal(initial_cash))
        if cash < ZERO:
            msg = f"initial cash must not be negative, got {cash}"
            raise MoneyError(msg)
        self._initial_cash = cash
        self._cash = cash
        self._positions: dict[str, Position] = {}
        self._applied_exec_ids: set[str] = set()
        self._allow_short = allow_short

    # -- read side ---------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def initial_cash(self) -> Decimal:
        return self._initial_cash

    @property
    def applied_fill_count(self) -> int:
        return len(self._applied_exec_ids)

    def positions(self) -> tuple[Position, ...]:
        """Positions sorted by instrument, so iteration order is deterministic."""
        return tuple(self._positions[key] for key in sorted(self._positions))

    def position(self, instrument: str) -> Position:
        """Current position, or a flat zero position when none is held."""
        return self._positions.get(instrument, Position(instrument, 0, ZERO, ZERO))

    def has_applied(self, exec_id: str) -> bool:
        return exec_id in self._applied_exec_ids

    def gross_exposure(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Sum of absolute market values. Missing marks are an error, never assumed zero."""
        total = ZERO
        for position in self.positions():
            if position.is_flat():
                continue
            total += abs(position.market_value(self._require_mark(marks, position.instrument)))
        return q_cash(total)

    def net_exposure(self, marks: Mapping[str, Decimal]) -> Decimal:
        total = ZERO
        for position in self.positions():
            if position.is_flat():
                continue
            total += position.market_value(self._require_mark(marks, position.instrument))
        return q_cash(total)

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        return q_cash(self._cash + self.net_exposure(marks))

    def weight_of(self, instrument: str, marks: Mapping[str, Decimal]) -> Decimal:
        """Current portfolio weight of ``instrument`` (0 when flat or equity is 0)."""
        equity = self.equity(marks)
        if equity == ZERO:
            return ZERO
        position = self.position(instrument)
        if position.is_flat():
            return ZERO
        return position.market_value(self._require_mark(marks, instrument)) / equity

    def digest(self) -> str:
        """Content digest of (cash, positions) for tying risk outcomes to state."""
        return digest_of(
            {
                "cash": self._cash,
                "positions": [
                    {
                        "instrument": p.instrument,
                        "quantity": p.quantity,
                        "average_cost": p.average_cost,
                        "realized_pnl": p.realized_pnl,
                    }
                    for p in self.positions()
                ],
            }
        )

    # -- write side --------------------------------------------------------------

    def apply_fill(
        self,
        *,
        exec_id: str,
        instrument: str,
        side: OrderSide,
        quantity: int,
        price: Decimal | str,
        commission: Decimal | str = ZERO,
        occurred_at: dt.datetime,
    ) -> FillResult | None:
        """Book one broker execution.

        Returns ``None`` when ``exec_id`` was already applied, so a duplicated broker
        event cannot double-book inventory or P&L (acceptance gate 7).
        """
        # Reject a naive timestamp at the accounting boundary: an unqualified instant in
        # a ledger entry is a correctness bug, not a formatting preference.
        ensure_utc(occurred_at)
        if exec_id in self._applied_exec_ids:
            return None
        shares = to_quantity(quantity)
        if shares <= 0:
            msg = f"fill quantity must be positive, got {shares}"
            raise MoneyError(msg)
        if not instrument:
            msg = "fill instrument must be non-empty"
            raise MoneyError(msg)
        fill_price = q_price(to_decimal(price))
        if fill_price <= ZERO:
            msg = f"fill price must be positive, got {fill_price}"
            raise MoneyError(msg)
        fees = q_cash(to_decimal(commission))
        if fees < ZERO:
            msg = f"commission must not be negative, got {fees}"
            raise MoneyError(msg)

        current = self.position(instrument)
        holding = current.quantity
        avg = current.average_cost
        realized = current.realized_pnl

        if side is OrderSide.BUY:
            new_qty = holding + shares
            gross = fill_price * shares
            # Commission is capitalised into the basis so that P&L is net of costs.
            new_avg = q_price((avg * holding + gross + fees) / new_qty) if new_qty else ZERO
            self._cash = q_cash(self._cash - gross - fees)
            realized_delta = ZERO
        else:
            if holding < shares and not self._allow_short:
                msg = (
                    f"refusing to book a short: sell of {shares} {instrument} against a "
                    f"position of {holding} (shorting is not permitted)"
                )
                raise MoneyError(msg)
            new_qty = holding - shares
            realized_delta = q_cash((fill_price - avg) * shares - fees)
            realized = q_cash(realized + realized_delta)
            self._cash = q_cash(self._cash + fill_price * shares - fees)
            new_avg = avg if new_qty != 0 else ZERO

        position = Position(
            instrument=instrument, quantity=new_qty, average_cost=new_avg, realized_pnl=realized
        )
        # Flat positions are retained when the instrument has trade history, so realized
        # P&L stays visible and audit re-computation can reproduce it.
        if not position.is_flat() or realized != ZERO or holding != 0:
            self._positions[instrument] = position
        elif realized == ZERO:
            self._positions.pop(instrument, None)

        self._applied_exec_ids.add(exec_id)
        return FillResult(
            instrument=instrument, position=position, cash=self._cash, realized_delta=realized_delta
        )

    def _require_mark(self, marks: Mapping[str, Decimal], instrument: str) -> Decimal:
        try:
            mark = to_decimal(marks[instrument])
        except KeyError as exc:
            msg = (
                f"no mark price for {instrument}: valuations are never defaulted to zero "
                "(a missing mark must fail closed)"
            )
            raise MoneyError(msg) from exc
        return q_price(mark)
