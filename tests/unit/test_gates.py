"""Gate verification: the coverage claim is machine-checkable, and it does not overstate.

Every round so far has asserted "the gates are met" in prose, in a progress file, by me. These
tests are about the report being trustworthy rather than flattering.

**THE MODULE CAUGHT ITS OWN AUTHOR ON THE FIRST RUN.** Ten mapped test files did not exist -- I
invented plausible filenames instead of reading the suite, and missed an entire `tests/fault/`
failure-injection directory where most of the cases are actually proven. It then caught a second
layer: the ids omitted class names, so they were rejected too. Both are the exact failure this
module exists to catch, and both are why the mapping is CHECKED rather than trusted.
"""

from __future__ import annotations

import pytest

from tigerquant.ops.gates import (
    CRITICAL_CASES,
    CaseResult,
    CaseStatus,
    CriticalCase,
    GateReport,
    _covers,
    verify_gates,
)


class TestTheMappingIsHonest:
    def test_every_case_is_mapped_to_something(self) -> None:
        """A case with no tests is allowed to EXIST -- it must simply not read as proven."""
        assert len(CRITICAL_CASES) == 16

    def test_the_numbers_are_one_to_sixteen_with_no_gaps(self) -> None:
        """The numbers must match docs/ACCEPTANCE_GATES.md, so a missing case is visible."""
        assert [case.number for case in CRITICAL_CASES] == list(range(1, 17))

    def test_every_mapped_test_actually_exists(self) -> None:
        """THE test that matters. A coverage table pointing at renamed-away tests is how a project
        convinces itself it is covered."""
        report = verify_gates()
        missing = [
            (result.case.number, result.missing) for result in report.results if result.missing
        ]
        assert missing == [], f"mapped tests that do not exist: {missing}"

    def test_the_suite_can_be_collected_at_all(self) -> None:
        """If collection fails the report says so rather than reporting every case as missing."""
        report = verify_gates()
        assert "COULD NOT COLLECT" not in report.note

    def test_no_case_is_reported_as_unproven(self) -> None:
        """The headline claim, asserted. If a case loses its tests this fails."""
        report = verify_gates()
        assert report.unproven == ()

    def test_the_partial_cases_name_what_is_missing(self) -> None:
        """'Case 4 is partial' is not actionable; the gap must be in words."""
        for result in verify_gates().partial:
            assert result.case.gap, f"case {result.case.number} is partial with no stated gap"

    def test_a_stated_caveat_or_gap_is_substantive(self) -> None:
        """A note that says nothing is worse than no note, because it LOOKS like disclosure.
        Cases with neither are fully proven, which is the normal state and needs no note.
        """
        for case in CRITICAL_CASES:
            for note in (case.gap, case.caveat):
                if note:
                    assert len(note) > 30, f"case {case.number} has a thin note: {note!r}"


class TestTheReportCannotContradictItself:
    def test_a_case_with_missing_tests_is_not_proven(self) -> None:
        """The first version rendered PROVEN while printing MISSING TEST(S) beneath it -- a report
        claiming 14/16 while ten cases pointed at nothing."""
        result = CaseResult(
            case=CriticalCase(number=99, summary="x", tests=("tests/unit/nope.py",)),
            status=CaseStatus.PROVEN,
            missing=("tests/unit/nope.py",),
        )
        assert result.effective_status == CaseStatus.UNPROVEN
        assert not result.ok

    def test_a_fully_present_case_keeps_its_claimed_status(self) -> None:
        result = CaseResult(
            case=CriticalCase(number=99, summary="x", tests=("tests/unit/a.py",)),
            status=CaseStatus.PROVEN,
            present=("tests/unit/a.py",),
        )
        assert result.effective_status == CaseStatus.PROVEN
        assert result.ok

    def test_the_rendered_line_uses_the_effective_status(self) -> None:
        """Otherwise the text and the evidence disagree, which is how a report misleads."""
        result = CaseResult(
            case=CriticalCase(number=99, summary="x", tests=("nope.py",)),
            status=CaseStatus.PROVEN,
            missing=("nope.py",),
        )
        assert "UNPROVEN" in result.describe()
        assert "PROVEN" not in result.describe().replace("UNPROVEN", "")

    def test_a_report_with_an_unproven_case_is_not_ok(self) -> None:
        result = CaseResult(case=CriticalCase(number=99, summary="x"), status=CaseStatus.UNPROVEN)
        assert not GateReport(results=(result,)).ok

    def test_partial_does_not_fail_the_report(self) -> None:
        """A case with a NAMED gap is honest. A gate that fails on every honest gap gets
        disabled, and a disabled gate protects nothing."""
        result = CaseResult(
            case=CriticalCase(number=99, summary="x", tests=("a",), gap="needs a real broker"),
            status=CaseStatus.PARTIAL,
            present=("a",),
        )
        assert GateReport(results=(result,)).ok


class TestCoverageMatching:
    def test_an_exact_node_id_matches(self) -> None:
        assert _covers("tests/unit/a.py::test_x", {"tests/unit/a.py::test_x"})

    def test_a_file_entry_matches_any_test_in_it(self) -> None:
        """Some cases are proven by a whole module, and that is a legitimate granularity."""
        assert _covers("tests/unit/a.py", {"tests/unit/a.py::TestC::test_x"})

    def test_a_class_entry_matches_its_methods(self) -> None:
        assert _covers("tests/unit/a.py::TestC", {"tests/unit/a.py::TestC::test_x"})

    def test_an_unknown_id_does_not_match(self) -> None:
        assert not _covers("tests/unit/nope.py", {"tests/unit/a.py::test_x"})

    def test_a_typo_does_not_match_by_prefix(self) -> None:
        """`tests/unit/a.py` must not match `tests/unit/ab.py`."""
        assert not _covers("tests/unit/a.py", {"tests/unit/ab.py::test_x"})


class TestTheCommandOutput:
    def test_the_summary_counts_every_case(self) -> None:
        report = verify_gates()
        total = len(report.proven) + len(report.partial) + len(report.unproven)
        assert total == 16

    def test_the_rendered_report_ends_with_the_honest_bottom_line(self) -> None:
        rendered = verify_gates().render()
        assert "critical test cases:" in rendered

    def test_partial_cases_are_labelled_in_the_render(self) -> None:
        rendered = verify_gates().render()
        assert "PARTIAL (2)" in rendered

    @pytest.mark.parametrize("case", CRITICAL_CASES, ids=lambda c: f"case-{c.number}")
    def test_every_case_description_is_readable(self, case: CriticalCase) -> None:
        assert case.summary
        assert len(case.summary) > 10
