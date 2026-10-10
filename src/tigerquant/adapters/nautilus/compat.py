"""Interface-shape contract for the pinned NautilusTrader wheel.

ADR-0010 requires this module to exist because the published documentation cannot be trusted
for this version. Verified during CR-00 and re-verified against the installed wheel in CR-04:
``https://nautilustrader.io/docs/latest/`` documents the v2.0.0rcN Rust-core line whose adapter
interface differs from 1.231.0, there is no versioned ``/docs/1.231.0/`` path, and even
``docs/developer_guide/adapters.md`` *inside the v1.231.0 tag* is already v2-oriented.

So the adapter is written against the tag's source, and this module pins what that source
actually provides. A wheel bump that moves a class, renames a method or changes the environment
enum fails here, loudly, instead of drifting silently into a subtly wrong adapter.

Every fact below was confirmed by importing the installed wheel, not by reading a guide. The
first probe of this work looked for ``Environment`` in ``nautilus_trader.common.enums`` and it
is not there — it lives in the pyo3 extension module. That is precisely the class of mistake
this file exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tigerquant.core.errors import IntegrationUnavailable

__all__ = [
    "EXPECTED_ENVIRONMENTS",
    "EXPECTED_EXECUTION_CLIENT_METHODS",
    "InterfaceCheck",
    "check_interface",
    "require_engine",
]

#: The pinned wheel. A mismatch is a compatibility failure, not a warning.
PINNED_VERSION = "1.231.0"

#: Where ``Environment`` actually lives in 1.231.0. Probed, not assumed.
ENVIRONMENT_MODULE = "nautilus_trader.core.nautilus_pyo3"

#: The engine's environments. Verified: there is no PAPER, which is the entire reason
#: ``adapters/nautilus/modes.py`` needs an explicit translation table.
EXPECTED_ENVIRONMENTS = frozenset({"BACKTEST", "LIVE", "SANDBOX"})

#: Methods an ExecutionClient subclass must be able to override in 1.231.0.
EXPECTED_EXECUTION_CLIENT_METHODS = (
    "submit_order",
    "cancel_order",
    "modify_order",
    "query_order",
    "generate_order_submitted",
    "generate_order_accepted",
    "generate_order_filled",
    "generate_order_rejected",
)

#: Methods a DataClient subclass must be able to override.
EXPECTED_DATA_CLIENT_METHODS = ("subscribe", "unsubscribe", "request")

#: Methods an InstrumentProvider subclass must be able to override.
EXPECTED_INSTRUMENT_PROVIDER_METHODS = (
    "initialize",
    "load_all_async",
    "load_ids_async",
    "add",
    "get_all",
)


@dataclass(frozen=True, slots=True)
class InterfaceCheck:
    """The outcome of probing the installed wheel."""

    available: bool
    version: str
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.available and not self.problems

    def summary(self) -> str:
        if not self.available:
            return f"NautilusTrader not installed ({'; '.join(self.problems)})"
        if self.problems:
            return f"NautilusTrader {self.version}: {len(self.problems)} interface problem(s)"
        return f"NautilusTrader {self.version}: interface contract satisfied"

    def raise_if_broken(self) -> None:
        if not self.ok:
            msg = (
                "the installed NautilusTrader does not match the interface this adapter was "
                "written against: " + "; ".join(self.problems)
            )
            raise IntegrationUnavailable("NautilusTrader", msg)


def require_engine() -> InterfaceCheck:
    """Probe the engine, or report that it is absent. Never raises for absence alone."""
    return check_interface()


def check_interface() -> InterfaceCheck:
    """Verify every fact the adapter depends on. Returns problems rather than raising."""
    problems: list[str] = []

    try:
        import nautilus_trader
    except ImportError as exc:
        return InterfaceCheck(
            available=False,
            version="(absent)",
            problems=(f"import failed: {type(exc).__name__}",),
        )

    version = getattr(nautilus_trader, "__version__", "(unknown)")
    if version != PINNED_VERSION:
        # Reported, not fatal: a newer wheel may still satisfy the surface, and the surface
        # checks below are what actually decide. But it is always visible.
        problems.append(f"version {version!r} is not the pinned {PINNED_VERSION!r}")

    # -- Environment: where it lives and what it contains ------------------------
    try:
        module = __import__(ENVIRONMENT_MODULE, fromlist=["Environment"])
        environment = module.Environment
        variants = {str(name) for name in environment.variants()}
        if variants != EXPECTED_ENVIRONMENTS:
            problems.append(
                f"Environment variants are {sorted(variants)}, expected "
                f"{sorted(EXPECTED_ENVIRONMENTS)}. The mode translation table in "
                "adapters/nautilus/modes.py is derived from the expected set and must be "
                "revisited (ADR-0010)."
            )
    except Exception as exc:  # noqa: BLE001 - any failure here is a compatibility problem
        problems.append(f"cannot read Environment from {ENVIRONMENT_MODULE}: {type(exc).__name__}")

    # -- the three override surfaces ---------------------------------------------
    problems.extend(
        _check_surface(
            "nautilus_trader.execution.client",
            "ExecutionClient",
            EXPECTED_EXECUTION_CLIENT_METHODS,
        )
    )
    problems.extend(
        _check_surface("nautilus_trader.data.client", "DataClient", EXPECTED_DATA_CLIENT_METHODS)
    )
    problems.extend(
        _check_surface(
            "nautilus_trader.common.providers",
            "InstrumentProvider",
            EXPECTED_INSTRUMENT_PROVIDER_METHODS,
        )
    )

    # -- identifiers the adapter maps onto our own types -------------------------
    try:
        from nautilus_trader.model.identifiers import (
            AccountId,
            ClientId,
            InstrumentId,
            Venue,
        )

        del AccountId, ClientId, InstrumentId, Venue
    except Exception as exc:  # noqa: BLE001
        problems.append(f"model.identifiers missing a required type: {type(exc).__name__}")

    # -- order enums we translate -------------------------------------------------
    try:
        from nautilus_trader.model.enums import (
            OrderSide,
            OrderStatus,
            OrderType,
            TimeInForce,
        )

        if not {member.name for member in OrderSide} >= {"BUY", "SELL", "NO_ORDER_SIDE"}:
            problems.append(f"OrderSide members changed: {[m.name for m in OrderSide]}")
        for enum, required in (
            (OrderType, {"MARKET", "LIMIT"}),
            (TimeInForce, {"DAY"}),
        ):
            names = {member.name for member in enum}
            if not names >= required:
                problems.append(f"{enum.__name__} lost a member we map: {sorted(required - names)}")
        if len(list(OrderStatus)) < 10:
            problems.append(f"OrderStatus has only {len(list(OrderStatus))} members")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"model.enums missing a required enum: {type(exc).__name__}")

    return InterfaceCheck(available=True, version=version, problems=tuple(problems))


def _check_surface(module_name: str, class_name: str, methods: tuple[str, ...]) -> list[str]:
    """Confirm a class exists and exposes every method an adapter must override."""
    try:
        module = __import__(module_name, fromlist=[class_name])
        cls: Any = getattr(module, class_name)
    except Exception as exc:  # noqa: BLE001
        return [f"{module_name}.{class_name} is missing: {type(exc).__name__}"]

    missing = [name for name in methods if not hasattr(cls, name)]
    if missing:
        return [
            f"{class_name} no longer provides {missing}. The adapter overrides these, so a "
            "wheel bump that removes one would silently stop intercepting that call."
        ]
    return []
