"""Cited evidence must exist.

Every status document points at `docs/evidence/*.txt` files, and **nothing checked that they are
there.** One commit message cited `docs/evidence/cr06_llm_transport_contract.txt` before that file
existed -- found by listing the paths named in the last five commits and testing each one, which no
check does.

**A citation to something absent is worse than no citation**, because it stops the reader looking
while appearing to have shown them. That is the same shape as everything else this project has been
finding: a claim nothing verifies.

This checks the DOCUMENTS rather than the commit messages, because the documents are the canonical
record and the commit log is not parsed anywhere. Commit messages are a remaining gap and are named
as one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

#: Documents that cite evidence.
CITING_DOCUMENTS = ("PROGRESS.md", "WORK_QUEUE.md", "DEPENDENCIES.md", "ACCEPTANCE_GATES.md")

_EVIDENCE_PATH = re.compile(r"docs/evidence/[A-Za-z0-9_\-.]+\.(?:txt|md|json)")


def _documents() -> list[Path]:
    return [DOCS / name for name in CITING_DOCUMENTS if (DOCS / name).is_file()]


def citations() -> dict[str, set[str]]:
    """Every evidence path cited, and which documents cite it."""
    found: dict[str, set[str]] = {}
    for document in _documents():
        for match in _EVIDENCE_PATH.finditer(document.read_text(encoding="utf-8")):
            found.setdefault(match.group(0), set()).add(document.name)
    return found


class TestCitedEvidenceExists:
    def test_the_documents_are_found(self) -> None:
        """Without this every assertion below passes on an empty set."""
        assert _documents(), "none of the citing documents were found"

    def test_at_least_one_citation_is_found(self) -> None:
        assert citations(), (
            "no evidence paths were parsed out of the status documents. The pattern is broken, so the "
            "existence check below would pass for any set of missing files."
        )

    def test_every_cited_evidence_file_exists(self) -> None:
        """THE check."""
        missing = {
            path: sorted(documents)
            for path, documents in citations().items()
            if not (ROOT / path).is_file()
        }
        assert missing == {}, (
            "these documents cite evidence that does not exist: "
            + "; ".join(
                f"{path} (cited by {', '.join(names)})" for path, names in sorted(missing.items())
            )
            + ". A citation to something absent stops the reader looking while appearing to have "
            "shown them."
        )

    def test_evidence_files_are_not_empty(self) -> None:
        """A zero-byte file satisfies existence and shows nothing."""
        empty = [
            path
            for path in citations()
            if (ROOT / path).is_file() and not (ROOT / path).read_text(encoding="utf-8").strip()
        ]
        assert empty == [], f"cited evidence files are empty: {sorted(empty)}"

    @pytest.mark.parametrize("name", CITING_DOCUMENTS)
    def test_each_document_still_exists(self, name: str) -> None:
        """Removing a citing document would silently shrink this check's coverage."""
        assert (DOCS / name).is_file(), f"{name} is gone, so its citations are no longer checked"
