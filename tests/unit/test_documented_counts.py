"""Documented test counts are verified against a real run, not trusted.

`docs/WORK_QUEUE.md` read "731 passed" for several rounds -- a lost update in a read-modify-write
script over a shared document. **Nothing detected it, because a stale test count is not a test
failure.** The document is prose, and prose was not checked.

The counts come from a `pytest_collection_finish` hook rather than from a child pytest process,
because **subprocess execution does not complete in this environment** -- confirmed with both pipes
and file redirection. A count from the run that actually happened is also the better fact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerquant.ops.gates import (
    _DOCUMENTED_COMMANDS,
    COLLECTED_COUNTS,
    DocumentedCount,
    GateReport,
    _collect_count,
    check_documented_counts,
    verify_gates,
)


class TestTheCountsMatch:
    def test_there_are_documented_counts_to_check(self) -> None:
        """If the document loses its count lines, that must be visible rather than silent."""
        assert len(check_documented_counts()) == len(_DOCUMENTED_COMMANDS)

    def test_every_configured_command_is_asserted_in_the_document(self) -> None:
        """A removed or reworded line reports claimed=-1, which cannot match a real count."""
        for count in check_documented_counts():
            assert count.claimed >= 0, f"{count.label!r} is no longer asserted in the document"

    def test_the_current_counts_match_a_real_run(self) -> None:
        """The live claim. If this fails, the documents are stale -- which is the point."""
        stale = [count.describe() for count in check_documented_counts() if not count.matches]
        assert stale == [], f"documented counts are stale: {stale}"

    def test_every_count_came_from_a_real_run(self) -> None:
        """-1 means no record, so this also asserts the hook ran."""
        for count in check_documented_counts():
            assert count.actual > 0, count.describe()

    def test_the_deselected_groups_account_for_the_whole_suite(self) -> None:
        """`not requires_db` plus `requires_db` must equal the total, or one of the three
        documented numbers is measuring something else."""
        by_marker = {count.marker: count.actual for count in check_documented_counts()}
        assert by_marker["not requires_db"] + by_marker["requires_db"] == by_marker[""]

    def test_the_report_is_ok(self) -> None:
        report = verify_gates()
        assert report.ok, report.render()

    @pytest.mark.parametrize("count", check_documented_counts(), ids=lambda c: c.label[:24])
    def test_each_documented_count_individually(self, count: DocumentedCount) -> None:
        assert count.matches, count.describe()


class TestAStaleCountIsCaught:
    def test_a_mismatch_does_not_match(self) -> None:
        """The exact historical failure: the document said 731 while the truth was 1044."""
        count = DocumentedCount(label="pytest -q", marker="", claimed=731, actual=1044)
        assert not count.matches
        assert "STALE" in count.describe()

    def test_the_description_shows_both_numbers(self) -> None:
        """ "Stale" alone is not actionable; the reader needs to see what it should be."""
        described = DocumentedCount(label="x", marker="", claimed=1, actual=2).describe()
        assert "claims 1" in described
        assert "actual 2" in described

    def test_a_stale_count_fails_the_whole_report(self) -> None:
        """Otherwise it is a warning nobody reads, which is how the original went unnoticed."""
        report = GateReport(counts=(DocumentedCount(label="x", marker="", claimed=1, actual=2),))
        assert not report.ok

    def test_the_summary_names_the_problem(self) -> None:
        report = GateReport(counts=(DocumentedCount(label="x", marker="", claimed=1, actual=2),))
        assert "STALE DOCUMENTED COUNTS" in report.summary()

    def test_the_render_lists_each_count(self) -> None:
        assert "documented count [OK]" in verify_gates().render()

    def test_a_matching_count_does_not_fail(self) -> None:
        report = GateReport(counts=(DocumentedCount(label="x", marker="", claimed=2, actual=2),))
        assert report.ok


class TestTheFailureModesTheCheckMustNotHave:
    def test_a_missing_record_is_minus_one_not_zero(self) -> None:
        """-1 cannot equal any real count, so an absent file FAILS the check rather than passing
        it -- which a default of 0 could have done for an empty suite."""
        assert _collect_count("", root=Path("no/such/place")) == -1

    def test_an_unknown_marker_is_minus_one(self) -> None:
        assert _collect_count("not a real marker") == -1

    def test_minus_one_never_matches(self) -> None:
        assert not DocumentedCount(label="x", marker="", claimed=-1, actual=0).matches
        assert not DocumentedCount(label="x", marker="", claimed=0, actual=-1).matches

    def test_a_missing_document_returns_no_counts_rather_than_raising(self, tmp_path: Path) -> None:
        """A gate command must not crash because a document moved."""
        assert check_documented_counts(root=tmp_path) == ()

    def test_a_document_with_no_count_lines_reports_claimed_minus_one(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "WORK_QUEUE.md").write_text("# nothing here\n", encoding="utf-8")
        counts = check_documented_counts(root=tmp_path)
        assert all(count.claimed == -1 for count in counts)
        assert all(not count.matches for count in counts)

    def test_a_corrupt_record_is_minus_one_not_an_exception(self, tmp_path: Path) -> None:
        (tmp_path / "out").mkdir()
        (tmp_path / COLLECTED_COUNTS).write_text("{not json", encoding="utf-8")
        assert _collect_count("", root=tmp_path) == -1

    def test_a_non_object_record_is_minus_one(self, tmp_path: Path) -> None:
        (tmp_path / "out").mkdir()
        (tmp_path / COLLECTED_COUNTS).write_text("[1, 2, 3]", encoding="utf-8")
        assert _collect_count("", root=tmp_path) == -1


class TestTheHookFeedsIt:
    def test_the_record_exists_after_a_run(self) -> None:
        """The suite running these tests wrote it during collection."""
        assert COLLECTED_COUNTS.is_file(), "the collection hook did not write its record"

    def test_the_record_carries_all_three_keys(self) -> None:
        record = json.loads(COLLECTED_COUNTS.read_text(encoding="utf-8"))
        for key in ("total", "not_requires_db", "requires_db"):
            assert key in record, f"the record is missing {key!r}"
            assert isinstance(record[key], int)

    def test_the_record_sees_the_requires_db_marker(self) -> None:
        """The first attempt reported requires_db=0 because the marker was not found. A
        module-level `pytestmark` IS reported by iter_markers, and this asserts it stays so."""
        record = json.loads(COLLECTED_COUNTS.read_text(encoding="utf-8"))
        assert record["by_marker"].get("requires_db", 0) > 0
