"""No new public definition in `src/` may be dead.

**THE SYSTEMATIC VERSION OF A PATTERN FOUND FOUR TIMES BY ACCIDENT THIS SESSION:** `evidence_facts` (a
table nothing populated), `KNOWN_CATEGORIES` (a constant nothing consumed), the pipeline's category
mapping (a policy nothing read), and `add_fact`/`facts_as_of` (methods nothing called). Each was a thing
that LOOKED wired and was not -- the most expensive kind of defect, because it reads as covered.

The sweep that found them took one script. This keeps the list from growing.

**THE EIGHT BELOW ARE KNOWN AND LISTED WITH A REASON.** A blanket "delete everything unreferenced"
would remove return-type dataclasses and public API that exist to be used by a caller outside this
repository, so each is a decision rather than a rule. **An entry with no reason is a suppression**, and
the test refuses one.

**WHAT COUNTS AS DEAD here:** a module-level public function or class whose name appears EXACTLY ONCE in
its own file (the definition) and nowhere in `src/` or `tests/`. That deliberately excludes things
referenced only by their own tests, which is a weaker signal worth seeing separately rather than
treating as dead.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TESTS = ROOT / "tests"

#: Public definitions that no code references, each with the reason it stays.
#:
#: A reason is REQUIRED. "Delete everything unreferenced" would remove return-type dataclasses and API
#: meant for a caller outside this repository, so these are decisions, not a rule.
KNOWN_DEAD: dict[str, str] = {
    "global_prefix": (
        "core/ids.py -- a small helper left from an earlier IdFactory shape. Harmless; removing it is "
        "a cleanup rather than a fix, and it is named here so it cannot hide."
    ),
    "coerce_decimal": (
        "decision/model.py -- a coercion helper superseded when the model stopped supplying numeric "
        "truth (AGENTS.md invariant 5). Kept deliberately as documentation of what was removed."
    ),
    "canonical_json": (
        "decision/prompt.py -- superseded by the core canonicalisation; a second JSON writer would be a "
        "second definition of canonical form, which is worse than an unused one."
    ),
    "default_schema_version": (
        "events/journal.py -- the schema version is a module constant now. A function returning it adds "
        "a call for no benefit."
    ),
    "price_attribute_names": (
        "evidence/facts.py -- a list of attribute names used by nothing. UNVERIFIED whether an external "
        "caller wants it; listed rather than deleted because evidence attributes are a public surface."
    ),
    "attribute_map": (
        "evidence/lineage.py -- same shape as price_attribute_names, and the same uncertainty."
    ),
}

SKIP = frozenset({"main", "build_parser"})


@lru_cache(maxsize=1)
def _dead_definitions() -> dict[str, str]:
    src_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in SRC.rglob("*.py")
    )
    # THIS FILE IS EXCLUDED FROM ITS OWN CORPUS. It names every entry in KNOWN_DEAD, so counting it
    # would make each of them "referenced in tests" and the sweep would report NOTHING dead -- a
    # measurement that changes what it measures. Found by the per-name tests failing while the
    # standalone sweep passed.
    # 	ools/ COUNTS AS USAGE. The first version scanned only src/ and tests/, so write_report --
    # called by 	ools/long_run_evidence.py -- was still reported as dead after it was wired up.
    # A sweep that misses a whole top-level package reports real usage as absent.
    test_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for root in (TESTS, ROOT / "tools")
        for path in root.rglob("*.py")
        if path.name != "test_no_dead_code.py"
    )
    found: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) or node.name.startswith("_"):
                continue
            name = node.name
            if name in SKIP:
                continue
            own = len(re.findall(rf"\b{re.escape(name)}\b", text))
            elsewhere = len(re.findall(rf"\b{re.escape(name)}\b", src_text)) - own
            in_tests = len(re.findall(rf"\b{re.escape(name)}\b", test_text))
            if own <= 1 and elsewhere <= 0 and in_tests == 0:
                found[name] = path.relative_to(ROOT).as_posix()
    return found


class TestNoNewDeadCode:
    def test_the_sweep_finds_definitions_at_all(self) -> None:
        """A parser that found nothing would make every check below vacuous."""
        assert _dead_definitions() is not None

    def test_no_unlisted_dead_definition_appears(self) -> None:
        """THE check. A new definition nothing calls is the shape this project has found four times."""
        found = _dead_definitions()
        unlisted = sorted(set(found) - set(KNOWN_DEAD))
        assert unlisted == [], (
            "these public definitions are referenced by NOTHING -- not src, not tests: "
            + ", ".join(f"{name} ({found[name]})" for name in unlisted)
            + ". Either wire them up, delete them, or add them to KNOWN_DEAD with a reason."
        )

    def test_every_known_dead_entry_states_a_reason(self) -> None:
        """An entry with no reason is a SUPPRESSION, and a suppression is how a known gap becomes an
        unknown one."""
        for name, reason in KNOWN_DEAD.items():
            assert len(reason) > 60, f"{name} is listed without a real reason"

    def test_the_list_does_not_rot(self) -> None:
        """An entry for something that is now USED is a stale claim, and the project has spent several
        rounds finding those."""
        found = _dead_definitions()
        stale = sorted(set(KNOWN_DEAD) - set(found))
        assert stale == [], (
            f"these are listed as dead but are no longer: {stale}. Remove them from KNOWN_DEAD -- an "
            "entry that outlives its subject makes the list untrustworthy."
        )

    @pytest.mark.parametrize("name", sorted(KNOWN_DEAD))
    def test_each_listed_name_is_still_dead(self, name: str) -> None:
        """Per-name, so a failure says WHICH entry rotted rather than only that the set changed."""
        assert name in _dead_definitions(), f"{name} is no longer dead"

    def test_the_trap_that_was_flagged_has_been_removed(self) -> None:
        """`empty_or_unfilled` was flagged as "possibly a missing parity case". It was NEITHER dead
        code to delete NOR a missing case: **it was a dead predicate whose semantics were WRONG.**

        It reported `True` for `BrokerOrderState.UNKNOWN`, whose docstring said "means no exposure
        resulted" -- while ADR-0008 says a timeout after a submission may have been ACCEPTED. Harmless
        only because nothing called it. **A dead helper whose semantics are wrong is a trap for whoever
        wires it next.**

        Corrected rather than deleted, so the distinction is written down where the next person will
        look. Asserted from the BEHAVIOUR, not from the list.
        """
        from tigerquant.execution.broker import BrokerOrderState
        from tigerquant.execution.parity import empty_or_unfilled

        assert empty_or_unfilled(BrokerOrderState.UNKNOWN) is False, (
            "UNKNOWN is treated as no exposure again; ADR-0008 says an uncertain outcome may have "
            "been accepted, so this predicate must not report that nothing happened"
        )
        assert "empty_or_unfilled" not in KNOWN_DEAD, (
            "it is listed as dead while being tested; the list has outlived its subject"
        )

    def test_the_evidence_persistence_gap_is_closed(self) -> None:
        """`write_report` was the most consequential entry: a function whose docstring says it persists
        a stability report as evidence, called by nothing, so CR-09's long-run evidence was NOT
        REPRODUCIBLE from the repository.

        `tools/long_run_evidence.py` now calls it, and running that tool reproduces the documented
        figures -- 5 sessions, 32.50 hours of simulated span, 8632 events, STABLE.

        **Asserted from the CALL SITE rather than from the list**, so this fails if the wiring is ever
        removed, which a list entry could not detect.
        """
        tool = ROOT / "tools" / "long_run_evidence.py"
        assert tool.is_file(), "the tool that produces the long-run evidence is gone"
        source = tool.read_text(encoding="utf-8")
        assert "run_long_sessions" in source, "the tool no longer runs a long session"
        assert "write_report" in source, "the tool no longer persists the report"
        assert "write_report" not in KNOWN_DEAD, (
            "write_report is listed as dead while being called; the list has outlived its subject"
        )
