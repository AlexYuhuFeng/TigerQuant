"""Closed enumerations shared by every layer.

These are the vocabulary of the audit journal, so their string values are part of the
on-disk contract and must not be renamed without a schema-version bump.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Action",
    "ImplementationStatus",
    "Interval",
    "Mode",
    "ModeCapabilities",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "ReasonCode",
    "RiskOutcomeKind",
    "Severity",
    "SnapshotCompleteness",
    "SubmissionTarget",
    "TimeInForce",
    "capabilities_for",
    "resolve_submission_target",
]


class Mode(StrEnum):
    """Operating mode. A mode is a deployment identity, never a runtime toggle."""

    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class ModeCapabilities:
    """What a process running in a given mode is permitted to do.

    Derived from :class:`Mode` (plus, for LIVE, a human-issued deployment
    authorization) rather than from configuration, so a mis-edited YAML file cannot
    grant a capability (ADR-0003).
    """

    mode: Mode
    can_read_broker: bool
    can_submit_orders: bool
    can_cancel_orders: bool
    requires_broker_credentials: bool
    requires_authorization: bool
    namespace: str


_CAPABILITIES: dict[Mode, ModeCapabilities] = {
    Mode.BACKTEST: ModeCapabilities(
        mode=Mode.BACKTEST,
        can_read_broker=False,
        can_submit_orders=False,
        can_cancel_orders=False,
        requires_broker_credentials=False,
        requires_authorization=False,
        namespace="bt",
    ),
    Mode.SHADOW: ModeCapabilities(
        mode=Mode.SHADOW,
        can_read_broker=True,
        # SHADOW observes only: hypothetical orders are simulated locally and are never
        # sent to any broker (docs/PRODUCT_SPEC.md "Modes").
        can_submit_orders=False,
        can_cancel_orders=False,
        requires_broker_credentials=True,
        requires_authorization=False,
        namespace="sh",
    ),
    Mode.PAPER: ModeCapabilities(
        mode=Mode.PAPER,
        can_read_broker=True,
        can_submit_orders=True,
        can_cancel_orders=True,
        requires_broker_credentials=True,
        requires_authorization=True,
        namespace="pa",
    ),
    Mode.LIVE: ModeCapabilities(
        mode=Mode.LIVE,
        can_read_broker=True,
        can_submit_orders=True,
        can_cancel_orders=True,
        requires_broker_credentials=True,
        # LIVE additionally requires a signed, human-issued deployment authorization
        # that coding agents must never create (AGENTS.md invariants 1 and 10).
        requires_authorization=True,
        namespace="lv",
    ),
}


def capabilities_for(mode: Mode, *, live_authorized: bool = False) -> ModeCapabilities:
    """Return the capability set for ``mode``.

    ``live_authorized`` defaults to ``False`` and is only ever ``True`` for a
    deployment that a human explicitly authorized outside of this codebase. Passing
    ``False`` for :attr:`Mode.LIVE` yields a capability set with every broker write
    capability removed, so an unauthorized live process is inert rather than
    dangerous.
    """
    caps = _CAPABILITIES[mode]
    if mode is Mode.LIVE and not live_authorized:
        return ModeCapabilities(
            mode=caps.mode,
            can_read_broker=False,
            can_submit_orders=False,
            can_cancel_orders=False,
            requires_broker_credentials=caps.requires_broker_credentials,
            requires_authorization=True,
            namespace=caps.namespace,
        )
    return caps


def resolve_submission_target(mode: Mode, broker_status: ImplementationStatus) -> SubmissionTarget:
    """Decide where an order would go, given the mode and the transport's real nature.

    The rules are deliberately restrictive:

    * A **simulated** transport is only usable from BACKTEST or SHADOW. A PAPER worker
      running against a simulator would produce ``PAPER``-looking results that were never
      paper-tested, which is exactly the false labelling invariant 11 forbids.
    * A **real** transport (paper or live verified) is only usable when the mode holds
      the order-submission capability — i.e. PAPER, or a knowingly authorized LIVE.
    * Anything unavailable, or a mismatch, resolves to :attr:`SubmissionTarget.NONE`.
    """
    caps = capabilities_for(mode)
    if broker_status is ImplementationStatus.SIMULATED:
        if mode in {Mode.BACKTEST, Mode.SHADOW}:
            return SubmissionTarget.SIMULATION
        return SubmissionTarget.NONE
    if broker_status in {ImplementationStatus.PAPER_VERIFIED, ImplementationStatus.LIVE_VERIFIED}:
        if caps.can_submit_orders:
            return SubmissionTarget.BROKER
        return SubmissionTarget.NONE
    return SubmissionTarget.NONE


class ImplementationStatus(StrEnum):
    """How much reality stands behind a component (AGENTS.md invariant 11)."""

    #: Deterministic local simulation. No external system was contacted.
    SIMULATED = "SIMULATED"
    #: Exercised against a broker's paper environment, with recorded evidence.
    PAPER_VERIFIED = "PAPER_VERIFIED"
    #: Exercised against a funded live account. Must never appear in this repository.
    LIVE_VERIFIED = "LIVE_VERIFIED"
    #: Configured but not reachable in this environment.
    UNAVAILABLE = "UNAVAILABLE"


class SubmissionTarget(StrEnum):
    """Where an order would physically go.

    This is deliberately separate from :class:`Mode`, because "the backtest placed an
    order" and "the paper worker sent an order to a broker" are completely different
    events that must never be confused (AGENTS.md invariant 11).
    """

    #: Nothing may be submitted at all.
    NONE = "NONE"
    #: Fills are produced by a local simulator; nothing leaves the process.
    SIMULATION = "SIMULATION"
    #: A real venue is contacted (Tiger paper in PAPER; live only if ever authorized).
    BROKER = "BROKER"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"
    GTC = "GTC"


class Interval(StrEnum):
    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    HOUR_1 = "1h"
    DAY_1 = "1d"


class Action(StrEnum):
    """Actions the decision model may propose (schemas/trade_decision.schema.json)."""

    NO_TRADE = "NO_TRADE"
    HOLD = "HOLD"
    SET_TARGET = "SET_TARGET"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


class OrderStatus(StrEnum):
    """Order intent states (ADR-0008). String values are part of the journal contract."""

    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"


class RiskOutcomeKind(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class SnapshotCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    CONTRADICTORY = "CONTRADICTORY"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ReasonCode(StrEnum):
    """Machine-readable denial/abstention reasons. Every denial carries one."""

    # Risk gateway
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    SHORT_NOT_ALLOWED = "SHORT_NOT_ALLOWED"
    MARGIN_NOT_ALLOWED = "MARGIN_NOT_ALLOWED"
    MAX_ORDER_FRACTION_EXCEEDED = "MAX_ORDER_FRACTION_EXCEEDED"
    MAX_SYMBOL_FRACTION_EXCEEDED = "MAX_SYMBOL_FRACTION_EXCEEDED"
    GROSS_EXPOSURE_EXCEEDED = "GROSS_EXPOSURE_EXCEEDED"
    INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
    MARK_STALE = "MARK_STALE"
    MARK_MISSING = "MARK_MISSING"
    MARKET_CLOSED = "MARKET_CLOSED"
    INSTRUMENT_HALTED = "INSTRUMENT_HALTED"
    RISK_HALTED = "RISK_HALTED"
    STARTUP_GATE_CLOSED = "STARTUP_GATE_CLOSED"
    MODE_CAPABILITY_ABSENT = "MODE_CAPABILITY_ABSENT"
    POLICY_MISSING = "POLICY_MISSING"
    ZERO_QUANTITY = "ZERO_QUANTITY"
    ORDER_RATE_EXCEEDED = "ORDER_RATE_EXCEEDED"
    DAILY_LOSS_EXCEEDED = "DAILY_LOSS_EXCEEDED"
    DRAWDOWN_EXCEEDED = "DRAWDOWN_EXCEEDED"
    TURNOVER_EXCEEDED = "TURNOVER_EXCEEDED"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    CHECK_ERROR = "CHECK_ERROR"
    # Decision / evidence
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SEMANTIC_INVALID = "SEMANTIC_INVALID"
    EVIDENCE_UNRESOLVED = "EVIDENCE_UNRESOLVED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EVIDENCE_CONTRADICTORY = "EVIDENCE_CONTRADICTORY"
    SNAPSHOT_INCOMPLETE = "SNAPSHOT_INCOMPLETE"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_VERSION_UNPINNED = "MODEL_VERSION_UNPINNED"
    NUMERIC_DISAGREEMENT = "NUMERIC_DISAGREEMENT"
    # Execution
    SUBMIT_TIMEOUT = "SUBMIT_TIMEOUT"
    SUBMIT_TRANSPORT_ERROR = "SUBMIT_TRANSPORT_ERROR"
    BROKER_REJECTED = "BROKER_REJECTED"
    UNRECONCILED = "UNRECONCILED"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    # Ops
    #: The supervised process is alive but not progressing. Distinct from a CRASH: a hung
    #: process still holds resources and may still hold a broker connection, so it cannot be
    #: treated as simply absent.
    SUPERVISOR_STALLED = "SUPERVISOR_STALLED"
    CLOCK_SKEW = "CLOCK_SKEW"
    LEASE_HELD_BY_OTHER = "LEASE_HELD_BY_OTHER"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
