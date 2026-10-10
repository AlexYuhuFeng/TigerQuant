"""Order execution: state machine, broker transports, and the execution service."""

from __future__ import annotations

from tigerquant.execution.broker import (
    BrokerAck,
    BrokerCancel,
    BrokerFill,
    BrokerOrderState,
    BrokerOrderView,
    BrokerProtocol,
)
from tigerquant.execution.fake_broker import DeterministicFakeBroker
from tigerquant.execution.intent import (
    ALLOWED_TRANSITIONS,
    BLOCKING_STATUSES,
    TERMINAL_STATUSES,
    OrderProjection,
    OrderSnapshot,
    fold_order,
    validate_transition,
)
from tigerquant.execution.service import (
    ExecutionService,
    ReconcileReport,
    StartupGate,
    SubmitResult,
    make_mark_provider,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BLOCKING_STATUSES",
    "TERMINAL_STATUSES",
    "BrokerAck",
    "BrokerCancel",
    "BrokerFill",
    "BrokerOrderState",
    "BrokerOrderView",
    "BrokerProtocol",
    "DeterministicFakeBroker",
    "ExecutionService",
    "OrderProjection",
    "OrderSnapshot",
    "ReconcileReport",
    "StartupGate",
    "SubmitResult",
    "fold_order",
    "make_mark_provider",
    "validate_transition",
]
