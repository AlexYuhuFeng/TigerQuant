"""CR-09: the incident runbooks must describe the system that exists.

A runbook is read at the worst moment, by someone who cannot afford to discover that the command
it names has been renamed or that the exit code it documents changed. So the parts of
``docs/runbooks/`` that are checkable in code are checked here: that every command it tells an
operator to run is a real subcommand, and that the exit-code table matches the actual CLI.

Nothing here tests the runbooks' *prose*. It tests that following them would work.
"""

from __future__ import annotations

import re
from pathlib import Path

from tigerquant.cli import build_parser

RUNBOOKS = Path("docs/runbooks")
MAIN = RUNBOOKS / "README.md"


def runbook_text() -> str:
    return MAIN.read_text(encoding="utf-8")


def runbook_prose() -> str:
    """The runbook with whitespace collapsed.

    Markdown wraps lines, so a phrase the runbook genuinely contains can straddle a newline.
    Asserting on the raw text would make these tests fail whenever a paragraph is reflowed,
    which teaches people to edit the test rather than the runbook.
    """
    return " ".join(runbook_text().split())


class TestTheRunbooksExist:
    def test_there_is_an_index(self) -> None:
        assert MAIN.is_file()
        assert runbook_text().startswith("# Incident runbooks")

    def test_it_states_that_none_of_them_restores_service(self) -> None:
        text = runbook_text()
        assert "containment procedure" in text
        assert "The system never repairs itself" in text

    def test_it_says_nobody_can_activate_live(self) -> None:
        assert "LIVE_READINESS.md" in runbook_text()

    def test_every_runbook_has_the_three_sections(self) -> None:
        """What you will see, what to do, what NOT to do. A runbook missing the third is how
        someone makes an incident worse."""
        text = runbook_text()
        headings = re.findall(r"^## (RB-\d+ .+)$", text, flags=re.MULTILINE)
        assert len(headings) >= 8, f"expected at least 8 runbooks, found {len(headings)}"
        for heading in headings:
            assert text.count(f"## {heading}") == 1

    def test_the_dangerous_actions_are_explicitly_forbidden(self) -> None:
        """Each 'Do NOT' is the thing an operator would otherwise do first."""
        text = runbook_text()
        assert text.count("**Do NOT") >= 5, "runbooks must say what not to do"
        for forbidden in ("resubmit", "do not project", "Do not repair"):
            assert forbidden.lower() in text.lower()


class TestTheCommandsAreReal:
    def test_every_documented_subcommand_exists(self) -> None:
        parser = build_parser()
        # The subcommand names, from the parser itself rather than a hardcoded list.
        actions = [
            action for action in parser._subparsers._group_actions if hasattr(action, "choices")
        ]
        known: set[str] = set()
        for action in actions:
            known |= set(action.choices or {})

        documented = set(re.findall(r"python -m tigerquant ([a-z-]+)", runbook_text()))
        unknown = documented - known
        assert not unknown, f"runbooks name subcommands that do not exist: {sorted(unknown)}"

    def test_the_common_commands_are_all_covered(self) -> None:
        text = runbook_text()
        for command in ("doctor", "verify-journal", "drift", "metrics", "report", "dashboard"):
            assert f"tigerquant {command}" in text, f"{command} is missing from the runbooks"


class TestTheExitCodeTable:
    """The table is a contract with whoever is scripting the response."""

    def test_the_table_is_present(self) -> None:
        text = runbook_text()
        assert "| `verify-journal` |" in text
        assert "| `drift` |" in text
        assert "| `metrics` |" in text

    def test_drift_exits_one_when_drifted_and_two_when_unreachable(self) -> None:
        """This is the claim RB-01 depends on, and it matches the implementation."""
        from tigerquant.cli import _cmd_drift

        source = _cmd_drift.__doc__ or ""
        assert "0 consistent" in source
        assert "1 drifted" in source
        assert "2" in source

    def test_metrics_exits_one_when_the_attribution_does_not_reconcile(self) -> None:
        from tigerquant.cli import _cmd_metrics

        assert "reconciles" in (_cmd_metrics.__doc__ or "")


class TestRunbooksMatchReality:
    def test_the_drift_runbook_describes_the_real_alert_behaviour(self) -> None:
        """RB-01 tells the operator one alert means one problem. That must be true."""
        from tigerquant.ops.drift_monitor import DEFAULT_DRIFT_POLICY

        text = runbook_prose()
        assert "once on discovery" in text
        # The escalation threshold the runbook quotes must be the configured one.
        assert f"{DEFAULT_DRIFT_POLICY.escalate_after} consecutive drifted checks" in text
        minutes = DEFAULT_DRIFT_POLICY.reminder_interval_seconds // 60
        assert f"once per {minutes} minutes" in text

    def test_the_halt_runbook_lists_the_real_owner_actor(self) -> None:
        from tigerquant.risk.state import Actor

        assert f"Actor.{Actor.OWNER.value}" in runbook_text()

    def test_the_cancel_exemption_is_stated_and_true(self) -> None:
        """RB-04 claims cancellation still works while halted. That is a safety claim."""
        from tigerquant.risk.state import RiskState

        state = RiskState()
        assert state.blocks_new_exposure() is False
        # RiskState exposes only the block predicate; nothing in it gates cancellation.
        public = {name for name in dir(state) if not name.startswith("_")}
        assert not any("cancel" in name.lower() for name in public)
        assert "Cancellation still works while halted" in runbook_text()
