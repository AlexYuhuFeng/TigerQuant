"""Every optional-extra marker must be SELECTED by some CI lane.

**105 tests ran in NO CI lane.** The extras job installs the broker extra and then selected only
`requires_engine or requires_llm`. Measured:

    requires_broker  -> 105 tests
    requires_engine  ->  31 tests
    requires_llm     ->   0 tests

And the 105 are the safety-critical ones: `test_guard_write_scope.py` and `test_tiger_execution.py`
cover `TigerAccountGuard` -- **the only thing keeping this project paper-only**, because the CR-02
OAuth grant returns a live-capable STANDARD account alongside the paper account. The guard that
prevents live trading had never been verified by CI.

A marker is easy to declare and easy to forget to select. That asymmetry is what this file checks.

**THE FIRST VERSION OF THIS FILE GAVE A FALSE PASS.** It searched the concatenated `run:` bodies for
the marker NAME -- and matched `requires_broker` inside a COMMENT explaining that `requires_broker`
was missing. It reported 8 passed against a deliberately reverted workflow. The lesson is the one
this session keeps teaching: **a check that greps prose is not a check.** This version parses the
`-m` EXPRESSION, strips comments first, and asserts the parser found something -- so a broken regex
fails loudly instead of passing everything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: Markers selecting tests that require an optional package.
EXTRA_MARKERS = ("requires_broker", "requires_engine", "requires_llm")

#: Markers selecting tests that require a running service.
SERVICE_MARKERS = ("requires_db",)


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run_bodies() -> list[str]:
    bodies: list[str] = []
    for job in workflow()["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str):
                bodies.append(run)
    return bodies


def _shell_code(body: str) -> str:
    """A run body with line continuations joined and COMMENT LINES REMOVED.

    Stripping comments is the whole point. The first version searched the raw body and matched a
    marker name inside an explanatory comment, reporting success against a broken workflow.
    """
    joined = body.replace("\\\n", " ")
    return "\n".join(line for line in joined.splitlines() if not line.lstrip().startswith("#"))


def expressions_in(body: str) -> list[str]:
    """The `-m` selections in ONE run body.

    Takes the body so it can be tested directly. The first version of the test below called the
    workflow-wide function on a synthetic string and compared it against the REAL workflow, so it
    could never pass -- and the workflow-wide function had to exist first for the name to resolve at
    all. Two layers of the same mistake: a test that could not express what it meant.
    """
    code = _shell_code(body)
    if "pytest" not in code:
        return []
    return [
        match.group(1) or match.group(2) or ""
        for match in re.finditer(r"""-m\s+(?:"([^"]*)"|'([^']*)')""", code)
    ]


def marker_expressions() -> list[str]:
    """Every `-m '...'` selection passed to pytest anywhere in the workflow. Prose cannot contribute."""
    return [expression for body in _run_bodies() for expression in expressions_in(body)]


def selected_markers() -> set[str]:
    """Which markers any lane actually SELECTS."""
    selected: set[str] = set()
    for expression in marker_expressions():
        selected.update(_positively_selected(expression))
    return selected


def _positively_selected(expression: str) -> set[str]:
    """Markers the expression INCLUDES, ignoring ones it excludes.

    **THE BUG THIS FIXES MADE THE WHOLE FILE VACUOUS.** The fast lane deselects with
    `not requires_db and not requires_broker and ...`, so a naive token scan found `requires_broker`
    in a DESELECTION and counted it as selected. The check then passed against a workflow whose
    extras lane had had `requires_broker` deliberately removed -- proved by reverting the workflow and
    watching it report 14 passed.

    A marker is selected only when it appears NOT negated. `not` binds to the token after it, so a
    marker directly preceded by `not` is excluded.
    """
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression)
    included: set[str] = set()
    for index, token in enumerate(tokens):
        if token in {"and", "or", "not"}:
            continue
        negated = index > 0 and tokens[index - 1] == "not"
        if not negated:
            included.add(token)
    return included


class TestTheParserWorks:
    """Without these, every check below could be vacuously true."""

    def test_the_workflow_parses(self) -> None:
        assert "jobs" in workflow()

    def test_some_lane_passes_a_marker_expression(self) -> None:
        assert marker_expressions(), (
            "no `-m` expression was found in any step. The parser is broken, so the marker checks "
            "below would pass against ANY workflow -- which is exactly what the first version did."
        )

    def test_comments_cannot_contribute_a_marker(self) -> None:
        """The specific false pass, asserted directly."""
        body = "pytest -q\n# we deliberately do not select requires_broker here\n"
        assert "requires_broker" not in _shell_code(body)

    def test_a_line_continued_selection_is_still_parsed(self) -> None:
        body = 'pytest -q \\\n  -m "requires_broker or requires_engine"\n'
        assert expressions_in(body) == ["requires_broker or requires_engine"]


class TestEveryMarkerIsSelected:
    def test_the_extras_job_installs_the_optional_extras(self) -> None:
        extras = workflow()["jobs"]["extras"]
        syncs = " ".join(
            str(step.get("run", ""))
            for step in extras["steps"]
            if step.get("name") == "Sync with every optional extra"
        )
        for extra in ("--extra broker", "--extra engine", "--extra llm"):
            assert extra in syncs, f"the extras job does not install {extra!r}"

    def test_every_extra_marker_is_selected(self) -> None:
        """THE check."""
        selected = selected_markers()
        missing = [marker for marker in EXTRA_MARKERS if marker not in selected]
        assert missing == [], (
            f"markers declared but SELECTED by no CI step: {missing}. Tests wearing them run NOWHERE, "
            "and `requires_broker` covers the guard that keeps this project paper-only."
        )

    def test_broker_tests_are_selected_named_explicitly(self) -> None:
        """The one that was missing, and whose reason is not obvious from the marker name."""
        assert "requires_broker" in selected_markers(), (
            "requires_broker is selected nowhere. 105 tests -- including TigerAccountGuard, the only "
            "thing preventing live trading -- would run in no CI lane."
        )

    def test_the_service_markers_are_accounted_for(self) -> None:
        """`requires_db` needs PostgreSQL, so a lane DESELECTS it rather than running it. Asserted so
        the omission is a DECISION rather than an oversight like the broker one.

        Checked against the raw expressions, NOT the positively-selected set: `requires_db` appears
        only as `not requires_db`, and the negation-aware parser correctly excludes it from that set.
        Asserting it there -- which the first version did -- asks the wrong question and passes or
        fails for the wrong reason.
        """
        expressions = marker_expressions()
        assert any("requires_db" in expression for expression in expressions), (
            "requires_db is neither run nor deselected in any lane, so its 20 tests are accounted "
            "for by nothing"
        )

    def test_a_negated_marker_is_not_counted_as_selected(self) -> None:
        """The property that makes the check above necessary, asserted directly so the two cannot
        drift into agreeing by accident."""
        assert _positively_selected("not requires_db and not requires_broker") == set()
        assert "requires_db" not in selected_markers(), (
            "requires_db is deselected everywhere and must not appear as SELECTED; if it does, the "
            "negation handling has regressed and the broker check is vacuous again"
        )


class TestNoStepPassesWhenItChecksNothing:
    def test_no_step_swallows_a_pytest_exit_code(self) -> None:
        """Exit 5 is 'no tests collected'. Tolerating it makes a step pass while checking nothing,
        which is how the missing broker selection stayed invisible."""
        offending = [
            line.strip()
            for body in _run_bodies()
            for line in _shell_code(body).splitlines()
            if "test $?" in line
        ]
        assert offending == [], (
            f"steps tolerate a pytest exit code: {offending}. That is how a lane whose markers "
            "stopped matching passes silently."
        )

    def test_the_extras_lane_has_no_fallback(self) -> None:
        extras = workflow()["jobs"]["extras"]
        step = next(
            s for s in extras["steps"] if s.get("name") == "Tests that need an optional extra"
        )
        assert "pytest" in step["run"]
        assert "||" not in step["run"], (
            "the extras lane has a fallback again; the selection must fail when it collects nothing, "
            "or the next missing marker is invisible too"
        )

    @pytest.mark.parametrize("marker", EXTRA_MARKERS + SERVICE_MARKERS)
    def test_each_marker_is_declared_in_pyproject(self, marker: str) -> None:
        """A selection naming an undeclared marker fails under `--strict-markers`, so the two files
        must agree."""
        declared = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert f'"{marker}:' in declared, (
            f"{marker} is selected in CI but not declared in pyproject"
        )
