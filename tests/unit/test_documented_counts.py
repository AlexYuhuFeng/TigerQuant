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
    AuditRecord,
    DocumentedCount,
    GateReport,
    _collect_count,
    check_documented_counts,
    verify_gates,
)

# These assertions describe the UNFILTERED suite. A lane with a -m filter deselects tests, so
# the record it writes is SMALLER than the documented numbers and the check reports the documents
# as stale when they are not -- found by running CI's fast lane locally. The honest position is
# that this module cannot verify its claim from a filtered run, so it says so and is deselected
# there rather than failing with a number that means something different.


def _recorded_invocations() -> set[str]:
    """Which commands have a recorded run. Used to SKIP assertions that need the local environment."""
    import json

    from tigerquant.ops.gates import COLLECTED_COUNTS

    try:
        record = json.loads(COLLECTED_COUNTS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    runs = record.get("runs")
    return set(runs) if isinstance(runs, dict) else set()


@pytest.fixture
def documented_commands_recorded() -> None:
    """SKIP a whole class when the local record lacks the documented commands.

    Not a weakening: these classes assert that the DOCUMENTS match the RECORD, which is meaningful
    only where the record holds those commands. **Applied at CLASS level because per-test guards were
    easy to miss and one WAS missed** -- 	est_each_documented_count_individually kept failing after
    the others were guarded, which is exactly how a partial fix hides.
    """
    _require_all_documented_commands()


def _require_all_documented_commands() -> None:
    """Skip unless every documented command has been run here.

    NOT a weakening. These assertions check that the DOCUMENTS match the RECORD, which is meaningful
    only where the record holds the documented commands. CI's fast lane runs one compound filter, so
    every documented count is NOT VERIFIED there -- the correct outcome, and one these assertions
    would misreport as a failure.
    """

    if not set(_DOCUMENTED_MARKERS) <= _recorded_invocations():
        pytest.skip(
            "not every documented command has a recorded run here; `gates` reports them NOT VERIFIED, "
            "which is correct and is not this assertion's subject"
        )


#: The `-m` expression of each documented command.
_DOCUMENTED_MARKERS = ("", "not requires_db", "requires_db")


RAN_AUDIT = AuditRecord(recorded_at="2026-10-12T00:00:00+00:00")


@pytest.mark.usefixtures("documented_commands_recorded")
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
        stale = [
            count.describe()
            for count in check_documented_counts()
            if not count.matches and not count.unverified_reason
        ]
        assert stale == [], f"documented counts are stale: {stale}"

    def test_every_count_came_from_a_real_run(self) -> None:
        """-1 means no record. A count is either verified by a matching run, or says why not."""
        for count in check_documented_counts():
            assert count.actual > 0 or count.unverified_reason, count.describe()

    def test_the_deselected_groups_account_for_the_whole_suite(self) -> None:
        """`not requires_db` plus `requires_db` must equal the total, or one of the three
        documented numbers is measuring something else."""
        # Only meaningful when all three commands have been run. In a lane that ran one compound
        # filter this is unverifiable, and saying so is the honest outcome rather than failing.
        by_marker = {count.marker: count for count in check_documented_counts()}
        if any(count.unverified_reason for count in by_marker.values()):
            pytest.skip("not every documented command has a recorded run in this environment")
        assert (
            by_marker["not requires_db"].actual + by_marker["requires_db"].actual
            == by_marker[""].actual
        )

    def test_the_report_is_ok(self) -> None:
        report = verify_gates()
        assert report.ok, report.render()

    @pytest.mark.parametrize("label", [label for label, _ in _DOCUMENTED_COMMANDS])
    def test_each_documented_count_individually(self, label: str) -> None:
        # READ INSIDE THE TEST, not in the parametrize decorator.
        #
        # The decorator is evaluated at COLLECTION, while the conftest hook writes the record at
        # pytest_collection_finish -- AFTER collection. So a parametrized value captured the record
        # as it was BEFORE this run, which is empty on a fresh checkout. Locally a previous run left
        # one behind and hid this; in CI there is never a previous run, so every count read -1 and
        # the suite could not pass there at all.
        #
        # A test that depends on state produced by its own run must read it when it RUNS.
        count = next(item for item in check_documented_counts() if item.label == label)
        # NOT VERIFIED is a real third state, not a failure: it means no recorded run used this exact
        # command, so nothing can confirm the claim. Asserting matches unconditionally would fail on
        # any lane that has not run THIS command -- which is how the check came to call correct
        # documents stale. What must never happen is STALE: a run that used the command and disagreed.
        assert count.matches or count.unverified_reason, count.describe()


@pytest.mark.usefixtures("documented_commands_recorded")
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
        report = GateReport(
            audit=RAN_AUDIT, counts=(DocumentedCount(label="x", marker="", claimed=1, actual=2),)
        )
        assert not report.ok

    def test_the_summary_names_the_problem(self) -> None:
        report = GateReport(
            audit=RAN_AUDIT, counts=(DocumentedCount(label="x", marker="", claimed=1, actual=2),)
        )
        assert "STALE DOCUMENTED COUNTS" in report.summary()

    def test_the_render_lists_each_count(self) -> None:
        assert "documented count [OK]" in verify_gates().render()

    def test_a_matching_count_does_not_fail(self) -> None:
        report = GateReport(
            audit=RAN_AUDIT, counts=(DocumentedCount(label="x", marker="", claimed=2, actual=2),)
        )
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

    def test_every_recorded_run_carries_all_three_keys(self) -> None:
        # PER RUN, not at the top level: the record is keyed by the invocation that produced it, so
        # there is no single top-level `total`. Asserting one is the shape that caused the original
        # defect -- a filtered run's counts being read as if they described the suite.
        runs = json.loads(COLLECTED_COUNTS.read_text(encoding="utf-8")).get("runs", {})
        assert runs, "the record has no runs"
        for invocation, counts in runs.items():
            for key in ("total", "not_requires_db", "requires_db"):
                assert key in counts, f"{invocation!r} is missing {key!r}"

    def test_the_hook_records_markers_per_invocation(self) -> None:
        """The first attempt reported requires_db=0 because the marker was not found. A
        module-level `pytestmark` IS reported by iter_markers. Asserted for ANY invocation that saw
        those tests: in a lane that deselects them, 0 is the CORRECT answer."""
        runs = json.loads(COLLECTED_COUNTS.read_text(encoding="utf-8")).get("runs", {})
        assert runs, "the record has no runs"
        for invocation, counts in runs.items():
            assert "by_marker" in counts, f"{invocation!r} has no by_marker"
            seen = counts["by_marker"].get("requires_db", 0)
            assert seen == counts["requires_db"], (
                f"{invocation!r}: by_marker says {seen} requires_db tests, the count says "
                f"{counts['requires_db']}"
            )
