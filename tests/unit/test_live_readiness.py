"""CR-10: the live-readiness document's factual claims must stay true.

A readiness document that quietly goes stale is worse than none, because it is the artifact a
human would rely on when deciding whether to attempt activation. So the claims in
``docs/LIVE_READINESS.md`` that are *checkable in code* are checked here. If someone weakens a
control, this fails rather than the document silently becoming a lie.

Nothing here activates anything, and nothing here can: every assertion is that a path is
**closed**.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerquant.adapters.nautilus.modes import describe_translation_table, translate
from tigerquant.core.enums import ImplementationStatus, Mode, SubmissionTarget, capabilities_for
from tigerquant.core.errors import ConfigurationError

READINESS = Path("docs/LIVE_READINESS.md")


class TestTheDocumentExists:
    def test_it_is_present_and_says_it_activates_nothing(self) -> None:
        text = READINESS.read_text(encoding="utf-8")
        assert text.startswith("# Live-readiness review")
        assert "NOT an activation" in text.splitlines()[0]
        assert "activates nothing" in text


class TestLiveRemainsOff:
    """The claims the document makes about live trading being unreachable."""

    def test_live_mode_cannot_translate_to_the_engine(self) -> None:
        with pytest.raises(ConfigurationError, match="never will in autonomous development"):
            translate(Mode.LIVE, submission_target=SubmissionTarget.BROKER)

    def test_live_mode_reports_no_submission_capability(self) -> None:
        assert capabilities_for(Mode.LIVE).can_submit_orders is False

    def test_no_mode_capability_path_reports_live_submission_without_authorization(self) -> None:
        """Every mode, unauthorised, must refuse to submit -- except PAPER, which is a venue."""
        for mode in Mode:
            caps = capabilities_for(mode)
            if mode is Mode.PAPER:
                continue
            assert not caps.can_submit_orders, f"{mode.value} claims it can submit"

    def test_the_readiness_table_never_claims_live_verification(self) -> None:
        assert all(row["status"] != "LIVE_VERIFIED" for row in describe_translation_table())

    def test_the_project_status_is_simulated(self) -> None:
        """While this is SIMULATED, the document's headline claim holds."""
        from tigerquant import IMPLEMENTATION_STATUS

        assert IMPLEMENTATION_STATUS is not ImplementationStatus.LIVE_VERIFIED

    def test_no_source_module_can_reach_a_live_venue(self) -> None:
        """A source scan, so a new module cannot quietly add live enablement."""
        from tigerquant.cli import _safety_checks

        names = {name for name, _ok, _detail in _safety_checks()}
        assert "no live enablement surface in source" in names
        assert all(ok for _name, ok, _detail in _safety_checks()), "a safety check is failing"


class TestTheDocumentDoesNotInstructActivation:
    """A readiness document must not read like a runbook for turning live on."""

    def test_it_contains_no_step_by_step_activation_procedure(self) -> None:
        lowered = READINESS.read_text(encoding="utf-8").lower()
        for phrase in (
            "to enable live, run",
            "set mode=live",
            "set mode = live",
            "export tigerquant_mode=live",
        ):
            assert phrase not in lowered, f"the document appears to instruct activation: {phrase!r}"

    def test_it_states_that_an_agent_may_not_activate(self) -> None:
        text = READINESS.read_text(encoding="utf-8")
        assert "No coding agent may enable live mode" in text

    def test_every_precondition_is_marked_not_done(self) -> None:
        """If one were marked done, this test should be updated deliberately, not drifted into."""
        text = READINESS.read_text(encoding="utf-8")
        rows = [
            line for line in text.splitlines() if line.startswith("| ") and "**Not done**" in line
        ]
        assert len(rows) >= 9, f"expected the precondition table, found {len(rows)} rows"


class TestIsolationClaims:
    """The document claims per-mode isolation at the database. That must remain true."""

    def test_modes_do_not_share_a_namespace(self) -> None:
        namespaces = {capabilities_for(mode).namespace for mode in Mode}
        assert len(namespaces) == len(list(Mode)), f"two modes share a namespace: {namespaces}"
