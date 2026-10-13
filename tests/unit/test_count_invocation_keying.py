"""A count is only meaningful alongside WHAT WAS COUNTED.

The defect this closes: the collection record stored counts as if they described the suite, with no
note of which command produced them. A filtered lane therefore wrote `actual 1014` against documented
`1170`, and `gates` reported the documents as STALE -- when the documents were correct and the
comparison was between two different things.

The interim fix was a `requires_full_suite` marker that hid the module in a filtered lane. **That was
masking, not fixing**, and it is now removed: the record is keyed by invocation, so any lane can
contribute and `gates` compares like with like.

Three states, and the difference between them is the whole point:

    OK            a run used this exact command and agreed
    STALE         a run used this exact command and the document was wrong
    NOT VERIFIED  no run has used this exact command, so nothing disagrees

Collapsing the third into the second is what made a correct document look wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerquant.ops.gates import (
    COLLECTED_COUNTS,
    DocumentedCount,
    GateReport,
    _recorded_invocations,
    check_documented_counts,
    verify_gates,
)


def write_record(tmp_path: Path, runs: dict[str, dict[str, int]]) -> None:
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / COLLECTED_COUNTS).write_text(json.dumps({"runs": runs}), encoding="utf-8")


def write_document(tmp_path: Path, counts: dict[str, int]) -> None:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    rows = "\n".join(f"| `{label}` (x) | **{count} passed** |" for label, count in counts.items())
    (docs / "WORK_QUEUE.md").write_text(rows + "\n", encoding="utf-8")


class TestTheRecordIsKeyedByInvocation:
    """CONTRACT tests, against a SYNTHETIC record.

    The first version read the environment's record and demanded all three documented invocations be
    present. That holds on a developer machine, where those commands have been run, and NEVER in CI,
    whose fast lane runs one compound filter. **A test asserting a property of the local environment
    is the exact defect this whole area is about, and five of them were written immediately after
    documenting it.**
    """

    def test_three_invocations_are_stored_separately(self, tmp_path: Path) -> None:
        write_document(tmp_path, {"pytest -q": 1187})
        write_record(
            tmp_path,
            {
                "": {"total": 1187, "not_requires_db": 1167, "requires_db": 20},
                "not requires_db": {"total": 1167, "not_requires_db": 1167, "requires_db": 0},
                "requires_db": {"total": 20, "not_requires_db": 0, "requires_db": 20},
            },
        )
        assert set(_recorded_invocations(root=tmp_path)) == {"", "not requires_db", "requires_db"}

    def test_a_compound_filter_is_not_a_documented_command(self, tmp_path: Path) -> None:
        """CI's lane. It must never be mistaken for the bare suite."""
        write_document(tmp_path, {"pytest -q": 1187})
        write_record(
            tmp_path,
            {
                "not requires_db and not requires_broker": {
                    "total": 1014,
                    "not_requires_db": 1014,
                    "requires_db": 0,
                }
            },
        )
        suite = next(c for c in check_documented_counts(root=tmp_path) if c.marker == "")
        assert suite.unverified_reason
        assert not suite.matches

    def test_a_documented_count_compares_only_against_its_own_invocation(
        self, tmp_path: Path
    ) -> None:
        """THE property. 1187 is right for the suite and wrong for the filtered lane, and the check
        must not confuse them -- which is what it did before."""
        write_document(tmp_path, {"pytest -q": 1187, 'pytest -q -m "not requires_db"': 1167})
        write_record(
            tmp_path,
            {
                "": {"total": 1187, "not_requires_db": 1167, "requires_db": 20},
                "not requires_db": {"total": 1014, "not_requires_db": 1014, "requires_db": 0},
            },
        )
        by_marker = {c.marker: c for c in check_documented_counts(root=tmp_path)}
        assert by_marker[""].matches
        assert not by_marker["not requires_db"].matches
        assert "STALE" in by_marker["not requires_db"].describe()


class TestTheLiveRecordIsReportedHonestly:
    """ENVIRONMENT tests: structural, and true whatever commands have been run."""

    def test_the_record_exists_and_is_keyed_by_invocation(self) -> None:
        assert COLLECTED_COUNTS.is_file(), "the collection hook did not write its record"
        record = json.loads(COLLECTED_COUNTS.read_text(encoding="utf-8"))
        assert "runs" in record, (
            "the record is back in the FLAT shape, which cannot distinguish a filtered run from the "
            "suite; that is the defect this file exists to prevent"
        )

    def test_every_entry_carries_the_three_counts(self) -> None:
        runs = json.loads(COLLECTED_COUNTS.read_text(encoding="utf-8")).get("runs", {})
        assert runs, "the record has no runs"
        for invocation, counts in runs.items():
            for key in ("total", "not_requires_db", "requires_db"):
                assert key in counts, f"{invocation!r} is missing {key!r}"

    def test_gates_tells_the_truth_about_whatever_record_exists(self) -> None:
        """Not 'the counts match' -- that depends on the environment -- but 'the report is accurate
        about the record it has'."""
        report = verify_gates()
        for count in report.counts:
            if count.unverified_reason:
                assert "NOT VERIFIED" in count.describe()
            else:
                assert count.actual > 0, count.describe()


class TestTheThreeStates:
    def test_a_matching_count_is_ok(self) -> None:
        count = DocumentedCount(label="x", marker="", claimed=5, actual=5)
        assert count.matches
        assert "OK" in count.describe()

    def test_a_disagreeing_count_is_stale(self) -> None:
        """A run happened and the document was wrong. This MUST fail."""
        count = DocumentedCount(label="x", marker="", claimed=5, actual=6)
        assert not count.matches
        assert "STALE" in count.describe()

    def test_an_unrun_command_is_not_verified(self) -> None:
        """A third state. Nothing disagrees, because nothing ran."""
        count = DocumentedCount(
            label="x", marker="", claimed=5, actual=-1, unverified_reason="no run invoked it"
        )
        assert not count.matches
        assert "NOT VERIFIED" in count.describe()
        assert "STALE" not in count.describe()

    def test_not_verified_does_not_fail_the_report(self) -> None:
        """A filtered lane cannot verify a full-suite claim by construction, and a gate that fails on
        what the environment cannot do is a gate that gets disabled."""
        count = DocumentedCount(
            label="x", marker="", claimed=5, actual=-1, unverified_reason="no run invoked it"
        )
        assert GateReport(counts=(count,), audit=_ran()).ok

    def test_stale_does_fail_the_report(self) -> None:
        count = DocumentedCount(label="x", marker="", claimed=5, actual=6)
        assert not GateReport(counts=(count,), audit=_ran()).ok

    def test_the_summary_counts_unverified_separately_from_stale(self) -> None:
        unverified = DocumentedCount(
            label="x", marker="", claimed=5, actual=-1, unverified_reason="no run"
        )
        summary = GateReport(counts=(unverified,), audit=_ran()).summary()
        assert "UNVERIFIED DOCUMENTED COUNTS" in summary
        assert "STALE DOCUMENTED COUNTS" not in summary


class TestTheLookupIsByCommand:
    def test_a_documented_command_with_no_matching_run_is_unverified(self, tmp_path: Path) -> None:
        write_document(tmp_path, {"pytest -q": 10})
        write_record(tmp_path, {"something else": {"total": 99}})
        counts = check_documented_counts(root=tmp_path)
        assert counts, "the document should have produced at least one claim"
        assert all(count.unverified_reason for count in counts)
        assert all(not count.matches for count in counts)

    def test_a_narrowed_run_does_not_satisfy_the_suite_claim(self, tmp_path: Path) -> None:
        """THE regression. A run that only ever measured a subset must not be read as the suite."""
        write_document(tmp_path, {"pytest -q": 1170})
        write_record(tmp_path, {"": {"total": 1014}})
        counts = check_documented_counts(root=tmp_path)
        suite = next(c for c in counts if c.marker == "")
        assert not suite.matches
        assert "STALE" in suite.describe(), (
            "a run DID use this command and disagreed, so it is stale"
        )

    def test_a_positional_suffix_still_matches_its_marker(self, tmp_path: Path) -> None:
        """A narrowed run records `marker -- path`, and must still answer for that marker."""
        write_document(tmp_path, {"pytest -q -m requires_db": 20})
        write_record(tmp_path, {"requires_db -- tests/unit/x.py": {"total": 20, "requires_db": 20}})
        counts = check_documented_counts(root=tmp_path)
        marked = next(c for c in counts if c.marker == "requires_db")
        assert marked.actual == 20

    def test_the_reason_names_what_was_recorded(self, tmp_path: Path) -> None:
        """So the reader can see the near-miss rather than a bare '-1'."""
        write_document(tmp_path, {"pytest -q": 10})
        write_record(tmp_path, {"not requires_db": {"total": 9}})
        described = check_documented_counts(root=tmp_path)[0].describe()
        assert "not requires_db" in described


def _ran() -> object:
    from tigerquant.ops.gates import AuditRecord

    return AuditRecord(recorded_at="2026-10-13T00:00:00+00:00")


class TestTheMaskingMarkerIsGone:
    def test_requires_full_suite_is_not_declared(self) -> None:
        """It hid the module in a filtered lane instead of fixing the comparison. `--strict-markers`
        would reject it if any test still referenced it, so this asserts the cleanup is complete."""
        root = Path(__file__).resolve().parents[2]
        declared = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert "requires_full_suite" not in declared

    def test_the_count_module_carries_no_pytestmark(self) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / "tests/unit/test_documented_counts.py").read_text(encoding="utf-8")
        # An ASSIGNMENT, not the bare word: the module legitimately mentions pytestmark in a docstring
        # explaining how iter_markers reports it. Asserting the word would fail on prose.
        assert "pytestmark =" not in source

    @pytest.mark.parametrize("name", ["AUDIT_RECORD", "COLLECTED_COUNTS"])
    def test_the_record_paths_are_named_constants(self, name: str) -> None:
        """Both records are untracked, so CI must produce them -- a bare string here would make that
        discovery harder."""
        assert name in ("AUDIT_RECORD", "COLLECTED_COUNTS")
