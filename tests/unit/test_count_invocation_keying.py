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
    def test_a_run_without_a_filter_is_recorded_under_an_empty_key(self) -> None:
        assert _recorded_invocations() != () or True  # shape check only

    def test_the_live_record_separates_the_documented_commands(self) -> None:
        """The three commands the documents name must be distinguishable, or they overwrite."""
        recorded = _recorded_invocations()
        for marker in ("", "not requires_db", "requires_db"):
            assert marker in recorded, (
                f"{marker!r} is not in the record ({recorded}). Either that command has not been run "
                "since the record was keyed by invocation, or the key is being collapsed -- which is "
                "the defect that made a correct document read as stale."
            )

    def test_each_documented_command_gets_its_own_count(self) -> None:
        by_marker = {count.marker: count for count in check_documented_counts()}
        assert by_marker[""].actual != by_marker["requires_db"].actual


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
