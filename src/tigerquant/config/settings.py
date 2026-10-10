"""Application configuration.

Configuration is *data that selects a deployment*, never an authorization. In
particular ``app.allow_live`` defaults to ``False`` and is not sufficient on its own to
enable anything: LIVE additionally requires a human-issued deployment authorization that
no code path in this repository can create (ADR-0003 §2).

The shape mirrors ``config/example.yaml`` exactly, so the documented example is the
authoritative schema rather than an approximation of it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tigerquant.core.canonical import digest_of
from tigerquant.core.enums import Mode, capabilities_for
from tigerquant.core.errors import ConfigurationError, LiveNotAuthorized

if TYPE_CHECKING:
    from tigerquant.core.enums import ModeCapabilities

__all__ = [
    "AppConfig",
    "AppSection",
    "BrokerConfig",
    "ModelConfig",
    "RiskPolicySection",
    "SecuritySection",
    "load_app_config",
]


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AppSection(_Section):
    """The ``app:`` block."""

    name: Annotated[str, Field(min_length=1)] = "TigerQuant"
    version: str = "0.2.0"
    environment: Mode = Mode.BACKTEST
    allow_live: bool = False
    #: Set only by a human-issued deployment authorization outside this repository.
    live_deployment_authorized: bool = False
    allowed_instruments: tuple[str, ...] = ()

    @field_validator("environment", mode="before")
    @classmethod
    def _normalise_mode(cls, value: object) -> object:
        """Accept ``paper`` as well as ``PAPER``.

        The on-disk vocabulary for modes is lower case (``config/example.yaml``) while the
        enum values — part of the journal contract — are upper case. Normalising here
        keeps both without making the enum case-insensitive everywhere.
        """
        if isinstance(value, str):
            return value.strip().upper()
        return value


class BrokerConfig(_Section):
    """The ``broker:`` block."""

    provider: Annotated[str, Field(min_length=1)] = "fake"
    account_alias: Annotated[str, Field(min_length=1)] = "none"
    #: Account type the adapter must observe *from the broker* before any write.
    #: Never trusted from this file alone (ADR-0003 §3).
    require_reported_account_type: Annotated[str, Field(min_length=1)] = "SIMULATED"
    execution_enabled: bool = False
    #: ``secret://`` reference, never a secret. See AGENTS.md "Security".
    secret_ref: str = ""


class ModelConfig(_Section):
    """The ``model:`` block. Aliases, never vendor names, in strategy code."""

    gateway: Annotated[str, Field(min_length=1)] = "none"
    alias: Annotated[str, Field(min_length=1)] = "unset"
    intended_provider: str = "none"
    intended_model: str = "none"
    allow_unreviewed_fallback_for_live: bool = False


class RiskPolicySection(_Section):
    """The ``risk:`` block. Illustrative baseline; CR-07 owns the full policy set."""

    gross_exposure_fraction: Decimal = Decimal("0.5")
    max_symbol_fraction: Decimal = Decimal("0.1")
    max_order_fraction: Decimal = Decimal("0.02")
    daily_loss_halt_fraction: Decimal = Decimal("0.01")
    allow_margin: bool = False
    allow_short: bool = False
    allow_options: bool = False

    @model_validator(mode="after")
    def _check_ranges(self) -> RiskPolicySection:
        for name in (
            "gross_exposure_fraction",
            "max_symbol_fraction",
            "max_order_fraction",
            "daily_loss_halt_fraction",
        ):
            value = getattr(self, name)
            if not (Decimal(0) <= value <= Decimal(1)):
                msg = f"risk.{name} must be within [0, 1], got {value}"
                raise ValueError(msg)
        return self


class SecuritySection(_Section):
    """The ``security:`` block. These two invariants are not configurable."""

    model_has_broker_write_access: bool = False
    live_activation_by_agent: bool = False

    @model_validator(mode="after")
    def _check_invariants(self) -> SecuritySection:
        if self.model_has_broker_write_access:
            msg = (
                "security.model_has_broker_write_access must be false: AI components have "
                "no broker write credentials (AGENTS.md invariant 3)"
            )
            raise ValueError(msg)
        if self.live_activation_by_agent:
            msg = (
                "security.live_activation_by_agent must be false: no coding agent may "
                "enable live trading (AGENTS.md invariant 1)"
            )
            raise ValueError(msg)
        return self


class AppConfig(BaseModel):
    """Top-level application configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app: AppSection = AppSection()
    broker: BrokerConfig = BrokerConfig()
    model: ModelConfig = ModelConfig()
    risk: RiskPolicySection = RiskPolicySection()
    security: SecuritySection = SecuritySection()

    @model_validator(mode="after")
    def _check_mode(self) -> AppConfig:
        if self.app.environment is Mode.LIVE:
            if not self.app.allow_live:
                msg = (
                    "app.environment: live requires app.allow_live: true (and human authorization)"
                )
                raise LiveNotAuthorized(msg)
            if not self.app.live_deployment_authorized:
                msg = (
                    "app.environment: live requires app.live_deployment_authorized: true, "
                    "which no coding agent may set (AGENTS.md invariants 1 and 10)"
                )
                raise LiveNotAuthorized(msg)
        if self.app.environment is Mode.PAPER and self.broker.execution_enabled:
            if self.broker.require_reported_account_type != "PAPER":
                msg = (
                    "paper mode with execution enabled must require a broker-reported "
                    "PAPER account; refusing to start"
                )
                raise ConfigurationError(msg)
        return self

    # -- convenience accessors ---------------------------------------------------

    @property
    def environment(self) -> Mode:
        return self.app.environment

    @property
    def allow_live(self) -> bool:
        return self.app.allow_live

    @property
    def allowed_instruments(self) -> tuple[str, ...]:
        return self.app.allowed_instruments

    def capabilities(self) -> ModeCapabilities:
        return capabilities_for(
            self.app.environment, live_authorized=self.app.live_deployment_authorized
        )

    def namespace(self, suffix: str) -> str:
        """Mode-scoped resource name (journal path, channel, lease, schema)."""
        return f"{self.capabilities().namespace}-{suffix}"

    def content_digest(self) -> str:
        """Digest recorded in the audit header so a run is tied to its configuration."""
        return digest_of(self)


def load_app_config(path: Path | str) -> AppConfig:
    """Load and strictly validate an application configuration file."""
    config_path = Path(path)
    if not config_path.is_file():
        msg = f"configuration file not found: {config_path}"
        raise ConfigurationError(msg)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"configuration {config_path} is not valid YAML: {exc}"
        raise ConfigurationError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"configuration {config_path} must contain a mapping at the top level"
        raise ConfigurationError(msg)
    try:
        return AppConfig.model_validate(raw)
    except LiveNotAuthorized:
        raise
    except Exception as exc:
        msg = f"configuration {config_path} failed validation: {exc}"
        raise ConfigurationError(msg) from exc
