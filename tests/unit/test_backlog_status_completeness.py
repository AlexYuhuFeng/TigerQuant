"""Every CR in the backlog must have a status row.

**FOUND BY MY OWN MISTAKE.** Reading the status table with a regex, I concluded CR-03 was MISSING --
the most safety-critical CR, the one blocked on an owner decision. It was there. The pattern required
`**PARTIAL**` and the row reads `**PARTIAL - BLOCKED on a write-scoped grant**`, so the em-dash and the
extra words broke the match and the row silently vanished from my reading.

A reader who made the same mistake would conclude the blocked CR did not exist. **Nothing checked that
the table is complete**, so nothing would have told either of us.

This asserts the set of CRs in the status table equals the set the backlog defines. A CR dropped from
the table is a CR whose status nobody can see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
QUEUE = ROOT / "docs" / "WORK_QUEUE.md"

#: The backlog this project committed to.
EXPECTED_CRS = tuple(f"CR-{index:02d}" for index in range(11))


def text() -> str:
    return QUEUE.read_text(encoding="utf-8")


def _table_row(cr: str) -> str | None:
    """The STATUS row for a CR: `| CR-nn | **<status>** | ...`.

    Matches between the pipes rather than against a status vocabulary. **The first version required a
    plain uppercase status word and missed CR-03**, whose status carries an em-dash and a clause. A
    reader is not going to know the vocabulary either.
    """
    for match in re.finditer(rf"^\| {re.escape(cr)} \|(.*)$", text(), re.MULTILINE):
        rest = match.group(1)
        if "**" in rest:
            return rest
    return None


def status_of(cr: str) -> str | None:
    row = _table_row(cr)
    if row is None:
        return None
    match = re.search(r"\*\*(.+?)\*\*", row)
    return match.group(1).strip() if match else None


class TestEveryCRHasAStatus:
    def test_the_document_was_found(self) -> None:
        assert QUEUE.is_file(), "WORK_QUEUE.md is gone, so every check here is vacuous"

    @pytest.mark.parametrize("cr", EXPECTED_CRS)
    def test_the_cr_has_a_status_row(self, cr: str) -> None:
        """THE check. A CR with no row is a CR whose status nobody can see."""
        assert _table_row(cr) is not None, (
            f"{cr} has no status row. It may still appear in the backlog table and in prose, but "
            "nothing in the STATUS table says where it stands."
        )

    @pytest.mark.parametrize("cr", EXPECTED_CRS)
    def test_the_status_is_non_empty(self, cr: str) -> None:
        status = status_of(cr)
        assert status, f"{cr} has a row but no bolded status"

    def test_no_extra_crs_are_claimed(self) -> None:
        """A row for a CR outside the backlog is a claim about work that was never scoped."""
        rowed = {
            match.group(1)
            for match in re.finditer(r"^\| (CR-\d+) \|(.*)$", text(), re.MULTILINE)
            if "**" in match.group(2)
        }
        assert rowed <= set(EXPECTED_CRS), (
            f"status rows for unscoped CRs: {sorted(rowed - set(EXPECTED_CRS))}"
        )


class TestTheParserSurvivesRealStatusValues:
    """The mistake, pinned. Each of these is a shape the table actually uses."""

    def test_a_status_with_an_em_dash_is_read(self) -> None:
        """CR-03 reads `PARTIAL - BLOCKED on a write-scoped grant`, and this is why it was missed."""
        assert status_of("CR-03"), "CR-03's status is unreadable; the em-dash form broke the parser"

    def test_a_status_with_a_clause_is_read(self) -> None:
        status = status_of("CR-03") or ""
        assert "BLOCKED" in status.upper(), (
            "CR-03 is blocked on an owner decision and its status must say so"
        )

    def test_the_plain_form_is_still_read(self) -> None:
        assert status_of("CR-07") == "COMPLETE"

    def test_a_readable_status_is_still_recognised_for_the_substantial_form(self) -> None:
        status = status_of("CR-05") or ""
        assert "SUBSTANTIALLY" in status.upper()
