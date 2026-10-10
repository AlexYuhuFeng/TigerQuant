"""Exception hierarchy.

Every exception raised by TigerQuant derives from :class:`TigerQuantError`, so the
pipeline can distinguish "a known, handled failure mode" from an unexpected bug. The
risk gateway in particular converts *any* unexpected exception into a DENY
(ADR-0007), which requires this separation to stay meaningful.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tigerquant.core.enums import ReasonCode

__all__ = [
    "BrokerError",
    "CanonicalizationError",
    "ConfigurationError",
    "DecisionValidationError",
    "EvidenceError",
    "IllegalOrderTransition",
    "IntegrationUnavailable",
    "JournalIntegrityError",
    "LiveNotAuthorized",
    "LookaheadError",
    "MissingMarketData",
    "ModeViolation",
    "MoneyError",
    "RiskDenied",
    "TigerQuantError",
]


class TigerQuantError(Exception):
    """Base class for all TigerQuant failures."""


class ConfigurationError(TigerQuantError):
    """Configuration is missing, malformed or internally inconsistent."""


class LiveNotAuthorized(ConfigurationError):
    """LIVE mode was requested without a human-issued deployment authorization.

    Raised at construction time so that an unauthorized live process cannot start,
    let alone trade (AGENTS.md invariant 1).
    """


class ModeViolation(TigerQuantError):
    """An operation was attempted that the current mode does not permit."""


class CanonicalizationError(TigerQuantError):
    """A value cannot be canonically serialised (e.g. ``float`` or ``set``).

    Floats and unordered collections are rejected because they would make hashes and
    replay non-deterministic (ADR-0011).
    """


class MoneyError(TigerQuantError):
    """Invalid monetary value, scale, or currency operation."""


class LookaheadError(TigerQuantError):
    """Code attempted to observe data that was not yet available.

    Raised by the point-in-time view when a caller asks for information beyond its
    ``as_of`` boundary. This is the executable form of the no-lookahead invariant
    (ADR-0005, acceptance gate 12).
    """


class MissingMarketData(TigerQuantError):
    """A required price/quote is absent. Always fail-closed, never interpolated."""


class EvidenceError(TigerQuantError):
    """Base class for evidence-resolution failures."""

    def __init__(self, message: str, reason_code: ReasonCode) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class DecisionValidationError(TigerQuantError):
    """A decision failed structural or semantic validation."""

    def __init__(self, message: str, reason_code: ReasonCode, *, stage: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.stage = stage


class RiskDenied(TigerQuantError):
    """The risk gateway refused an intent. Never downgraded to a warning."""

    def __init__(self, message: str, reason_codes: Iterable[ReasonCode]) -> None:
        super().__init__(message)
        self.reason_codes: tuple[ReasonCode, ...] = tuple(reason_codes)
        if not self.reason_codes:
            msg = "RiskDenied requires at least one reason code"
            raise ValueError(msg)


class IllegalOrderTransition(TigerQuantError):
    """An order-intent state transition not permitted by ADR-0008."""


class JournalIntegrityError(TigerQuantError):
    """The append-only journal fails hash-chain or structural verification."""


class BrokerError(TigerQuantError):
    """A broker interaction failed. Not used for "unknown outcome" — see ADR-0008."""


class IntegrationUnavailable(TigerQuantError):
    """An optional integration dependency is not installed.

    Raised instead of silently degrading to a stub, so missing functionality is never
    hidden behind something that looks production-ready (AGENTS.md invariant 11).
    """

    def __init__(self, component: str, extra: str) -> None:
        super().__init__(
            f"{component} requires the optional dependency group '{extra}'. "
            f"Install with: pip install -e '.[{extra}]'"
        )
        self.component = component
        self.extra = extra
