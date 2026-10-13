"""Evidence written must be evidence CITED.

`test_evidence_citations.py` checks the FORWARD direction: every evidence path a document cites exists.
**Nothing checked the REVERSE**, and eight files were orphaned -- written and referenced by no document
at all, five of them from a single session.

**EVIDENCE NOBODY CAN FIND IS EVIDENCE NOBODY HAS.** An orphan is indistinguishable from a file that
was never written, except that it costs the reader the effort of finding out.

Neither direction alone would have caught the larger failure: **three rounds shipped with no evidence
file at all**, so there was nothing for either check to see. That is recorded in PROGRESS rather than
fixed here, because detecting "a round happened and recorded nothing" needs the commit history, and
this file checks the workspace.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "evidence"

#: Documents that may cite evidence. PROGRESS and WORK_QUEUE are the canonical ones; the rest are
#: accepted because they are read by an operator rather than a developer.
CITING = (
    "docs/PROGRESS.md",
    "docs/WORK_QUEUE.md",
    "docs/DEPENDENCIES.md",
    "docs/ACCEPTANCE_GATES.md",
    "docs/LIVE_READINESS.md",
    "README.md",
)

_EVIDENCE_PATH = re.compile(r"docs/evidence/[A-Za-z0-9_\-.]+\.txt")


def evidence_files() -> set[str]:
    return {f"docs/evidence/{path.name}" for path in EVIDENCE.glob("*.txt")}


def cited_files() -> set[str]:
    cited: set[str] = set()
    for name in CITING:
        path = ROOT / name
        if path.is_file():
            cited |= set(_EVIDENCE_PATH.findall(path.read_text(encoding="utf-8")))
    return cited


class TestNoOrphanedEvidence:
    def test_the_evidence_directory_is_found(self) -> None:
        """Without this every assertion below passes on an empty set."""
        assert EVIDENCE.is_dir(), "the evidence directory is gone"
        assert evidence_files(), "no evidence files were found, so this check is vacuous"

    def test_something_is_cited(self) -> None:
        """If the parser broke, the orphan check would report EVERY file as an orphan -- loud, but for
        the wrong reason."""
        assert cited_files(), "no citations were parsed out of any document"

    def test_every_evidence_file_is_cited_somewhere(self) -> None:
        """THE check."""
        orphans = sorted(evidence_files() - cited_files())
        assert orphans == [], (
            "these evidence files are cited by NO document, so a reader has no path to them: "
            + ", ".join(orphans)
            + ". Cite them from PROGRESS.md or WORK_QUEUE.md, or delete them."
        )

    def test_the_newest_evidence_is_cited(self) -> None:
        """A weaker, targeted form of the same check: the most recently MODIFIED files are the ones a
        round just wrote, and they are the ones most likely to be orphaned."""
        newest = sorted(
            EVIDENCE.glob("*.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:5]
        orphans = [
            f"docs/evidence/{path.name}"
            for path in newest
            if f"docs/evidence/{path.name}" not in cited_files()
        ]
        assert orphans == [], f"recently written evidence is orphaned: {orphans}"

    @pytest.mark.parametrize("document", CITING)
    def test_each_citing_document_exists_or_is_absent_deliberately(self, document: str) -> None:
        """Removing a citing document silently shrinks this check's coverage. README.md at the root is
        the only one allowed to be absent, because it is a package readme in some layouts."""
        path = ROOT / document
        if document == "README.md":
            return
        assert path.is_file(), f"{document} is gone, so its citations are no longer checked"
