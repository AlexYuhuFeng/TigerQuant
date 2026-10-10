"""Configuration and mode isolation: live cannot be enabled by any code path."""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerquant.config.settings import AppConfig, SecuritySection, load_app_config
from tigerquant.core.enums import (
    ImplementationStatus,
    Mode,
    SubmissionTarget,
    capabilities_for,
    resolve_submission_target,
)
from tigerquant.core.errors import ConfigurationError, LiveNotAuthorized

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestModeCapabilities:
    def test_backtest_and_shadow_cannot_submit(self) -> None:
        for mode in (Mode.BACKTEST, Mode.SHADOW):
            assert capabilities_for(mode).can_submit_orders is False

    def test_live_requires_authorization_and_is_inert_without_it(self) -> None:
        unauthorized = capabilities_for(Mode.LIVE)
        assert unauthorized.can_submit_orders is False
        assert unauthorized.requires_authorization is True
        assert capabilities_for(Mode.LIVE, live_authorized=True).can_submit_orders is True

    def test_namespaces_are_distinct(self) -> None:
        namespaces = {capabilities_for(mode).namespace for mode in Mode}
        assert len(namespaces) == len(list(Mode))


class TestSubmissionTargetResolution:
    def test_simulated_broker_is_only_usable_in_backtest_or_shadow(self) -> None:
        assert (
            resolve_submission_target(Mode.BACKTEST, ImplementationStatus.SIMULATED)
            is SubmissionTarget.SIMULATION
        )
        assert (
            resolve_submission_target(Mode.SHADOW, ImplementationStatus.SIMULATED)
            is SubmissionTarget.SIMULATION
        )
        assert (
            resolve_submission_target(Mode.PAPER, ImplementationStatus.SIMULATED)
            is SubmissionTarget.NONE
        )

    def test_verified_transport_needs_the_submission_capability(self) -> None:
        assert (
            resolve_submission_target(Mode.PAPER, ImplementationStatus.PAPER_VERIFIED)
            is SubmissionTarget.BROKER
        )
        assert (
            resolve_submission_target(Mode.BACKTEST, ImplementationStatus.PAPER_VERIFIED)
            is SubmissionTarget.NONE
        )
        assert (
            resolve_submission_target(Mode.LIVE, ImplementationStatus.LIVE_VERIFIED)
            is SubmissionTarget.NONE
        )

    def test_unavailable_transport_is_none(self) -> None:
        assert (
            resolve_submission_target(Mode.PAPER, ImplementationStatus.UNAVAILABLE)
            is SubmissionTarget.NONE
        )


class TestAppConfig:
    def test_backtest_default_is_safe(self) -> None:
        config = AppConfig()
        assert config.environment is Mode.BACKTEST
        assert config.allow_live is False
        assert config.app.live_deployment_authorized is False

    def test_live_without_allow_live_is_refused(self) -> None:
        with pytest.raises(LiveNotAuthorized):
            AppConfig.model_validate({"app": {"environment": "live", "allow_live": False}})

    def test_live_with_authorization_flag_is_refused(self) -> None:
        with pytest.raises(LiveNotAuthorized):
            AppConfig.model_validate(
                {
                    "app": {
                        "environment": "live",
                        "allow_live": True,
                        "live_deployment_authorized": False,
                    }
                }
            )

    def test_paper_with_execution_requires_a_paper_account(self) -> None:
        with pytest.raises(ConfigurationError, match="PAPER account"):
            AppConfig.model_validate(
                {
                    "app": {"environment": "paper"},
                    "broker": {
                        "execution_enabled": True,
                        "require_reported_account_type": "SIMULATED",
                    },
                }
            )

    def test_paper_with_execution_and_paper_account_is_accepted(self) -> None:
        config = AppConfig.model_validate(
            {
                "app": {"environment": "paper"},
                "broker": {
                    "execution_enabled": True,
                    "require_reported_account_type": "PAPER",
                },
            }
        )
        assert config.capabilities().can_submit_orders

    def test_model_broker_write_access_is_refused(self) -> None:
        with pytest.raises(Exception, match="no broker write credentials"):
            SecuritySection(model_has_broker_write_access=True)

    def test_agent_live_activation_is_refused(self) -> None:
        with pytest.raises(Exception, match="no coding agent"):
            SecuritySection(live_activation_by_agent=True)

    def test_namespace_is_mode_scoped(self) -> None:
        paper = AppConfig.model_validate({"app": {"environment": "paper"}})
        backtest = AppConfig()
        assert paper.namespace("journal") != backtest.namespace("journal")

    def test_digest_is_stable(self) -> None:
        assert AppConfig().content_digest() == AppConfig().content_digest()


class TestConfigLoading:
    def test_example_config_loads(self) -> None:
        config = load_app_config(EXAMPLE)
        assert config.allow_live is False
        assert config.broker.execution_enabled is False

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            load_app_config(tmp_path / "nope.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, "app: [unclosed")
        with pytest.raises(ConfigurationError):
            load_app_config(path)

    def test_non_mapping_root(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, "- just\n- a list\n")
        with pytest.raises(ConfigurationError, match="mapping"):
            load_app_config(path)

    def test_live_config_is_refused_at_load(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, "app:\n  environment: live\n  allow_live: true\n")
        with pytest.raises(LiveNotAuthorized):
            load_app_config(path)
