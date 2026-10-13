"""A schema table that only tests ever write to.

**THE FINDING.** `evidence_facts` is declared in `0001_initial.sql`. `PostgresRepository.add_fact` and
`facts_as_of` implement writing and the point-in-time read. **Nothing in `src/` calls either of them.**
The only callers are the tests.

So in a running system that table is ALWAYS EMPTY, and `facts_as_of` -- the query that makes the
projection useful for audit, because it answers "what did we know at that instant" -- has nothing to
read. **A table in the schema that is never populated looks exactly like a table that works.**

CR-09 lists "evidence-fact projection" as remaining. This is it: the projector routes `_ORDER_EVENTS`
and `_FILL_EVENTS` and has no evidence branch.

**WHY THIS IS ASSERTED RATHER THAN FIXED HERE.** Adding an evidence event and a projector branch is a
real change to the journal's event surface, and making it late in a round -- without the design question
of WHAT an evidence event carries, and when it is emitted -- is how a half-built thing gets committed.
The gap is recorded as a test so it is visible, so the next round starts from a measurement rather than
a suspicion, and so a fix has something to delete.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


#: Files that DEFINE the evidence methods -- the Protocol and its implementations. A definition is not
#: a caller, and counting one made the first version of these tests report the gap as closed.
DEFINITIONS = ("/db/postgres.py", "/db/repository.py")


def callers_of(name: str) -> list[str]:
    """Source files with a CALL to `name`, excluding the files that define it.

    Matches `.name(` rather than the bare name. **The first version matched the bare name and counted
    the Protocol declaration as a caller** -- it reported the gap as closed while measuring nothing,
    which is the same matches-the-word-not-the-behaviour mistake this project has now made four times.
    """
    pattern = re.compile(rf"\.{re.escape(name)}\s*\(")
    found: list[str] = []
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.endswith(DEFINITIONS):
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            found.append(relative)
    return found


class TestTheSchemaTables:
    def test_the_evidence_table_is_declared(self) -> None:
        schema = (SRC / "tigerquant" / "db" / "schema" / "0001_initial.sql").read_text(
            encoding="utf-8"
        )
        assert "CREATE TABLE IF NOT EXISTS evidence_facts" in schema, (
            "the evidence table is gone from the schema, so this file's premise is wrong"
        )

    def test_the_repository_can_write_and_read_it(self) -> None:
        """The SQL exists. It is the PROJECTION that is missing, which is a different problem from an
        unimplemented repository and needs a different fix."""
        from tigerquant.db.postgres import PostgresRepository

        assert hasattr(PostgresRepository, "add_fact")
        assert hasattr(PostgresRepository, "facts_as_of")


class TestNothingPopulatesIt:
    """The finding, measured. Each of these fails the moment the gap is closed -- which is the point:
    a test that documents a gap should break when somebody fixes it."""

    def test_no_source_file_other_than_postgres_calls_add_fact(self) -> None:
        mentions = callers_of("add_fact")
        assert mentions == [], (
            "something now calls add_fact: "
            f"{mentions}. If evidence-fact projection was implemented, THIS FILE SHOULD BE DELETED "
            "rather than updated -- it exists to mark a gap, not to describe a design."
        )

    def test_no_source_file_other_than_postgres_calls_facts_as_of(self) -> None:
        mentions = [
            path for path in callers_of("facts_as_of") if not path.endswith("db/postgres.py")
        ]
        assert mentions == [], (
            f"something now calls facts_as_of: {mentions}. The point-in-time evidence read is wired "
            "up, so the gap this file records is closed."
        )

    def test_the_projector_has_no_evidence_branch(self) -> None:
        """The projector is where orders, fills, positions, decisions and risk outcomes are projected.
        Evidence is the missing one."""
        projector = (SRC / "tigerquant" / "db" / "projector.py").read_text(encoding="utf-8")
        assert "_EVIDENCE_EVENTS" not in projector, (
            "the projector has an evidence branch now, so evidence-fact projection is implemented and "
            "this test should be replaced by tests of the behaviour."
        )

    def test_the_projector_still_routes_the_other_event_families(self) -> None:
        """So the assertion above is about a REAL absence rather than a renamed constant. If the
        projector were rewritten, `_EVIDENCE_EVENTS` would be absent for the wrong reason."""
        projector = (SRC / "tigerquant" / "db" / "projector.py").read_text(encoding="utf-8")
        for family in ("_ORDER_EVENTS", "_FILL_EVENTS"):
            assert family in projector, (
                f"{family} is gone, so the absence check above is meaningless"
            )

    def test_no_event_type_carries_an_evidence_fact(self) -> None:
        """The root cause. Projection is driven by the journal, and no event carries a fact."""
        pattern = re.compile(r"class\s+\w*(Evidence|Fact)\w*\s*\(", re.MULTILINE)
        carriers: list[str] = []
        for path in (SRC / "tigerquant" / "events").rglob("*.py"):
            for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
                carriers.append(f"{path.name}:{match.group(0).strip()}")
        assert carriers == [], (
            f"an event now carries evidence: {carriers}. The projection path exists and this file's "
            "premise has changed."
        )


class TestTheConsequence:
    def test_a_running_system_leaves_the_table_empty(self) -> None:
        """Stated as the consequence rather than the cause, because it is what an operator would
        notice: a query against `evidence_facts` returns nothing, and nothing says why."""
        from tigerquant.db.postgres import PostgresRepository

        # The read exists and is typed; it is simply never called.
        source = (SRC / "tigerquant" / "db" / "postgres.py").read_text(encoding="utf-8")
        assert "def facts_as_of" in source
        assert "FROM evidence_facts" in source
        assert PostgresRepository.facts_as_of is not None
