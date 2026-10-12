"""The dependency audit is recorded, reported, and its accepted risk is EXPLICIT.

The problem this solves is the one that hid the vulnerability for several rounds: the audit was
recorded as "cannot run" ONCE and never retested. **A check that lives in someone's memory rots the
same way a documented test count does.**

Design, following the count check:

* `gates --record-audit` runs pip-audit and writes what it found;
* every later `gates` run REPORTS it -- fast, offline, and in the same output as everything else;
* **NO RECORD IS A FAILURE, not a pass.** An audit that has never run is not a passing audit.

And one thing this round found the hard way: **an UPSTREAM fix is not a fix we can APPLY.**
`pip-audit` reports `fix: 2026.6.0` for CVE-2026-104851, but `nautilus_trader==1.231.0` pins
`fsspec==2026.2.0` exactly and is the latest release. My first version marked it "fixable" and the
gate exited 1 -- **for ever, with no action that could satisfy it.** A gate that fails on the
unactionable gets disabled, and a disabled gate protects nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerquant.ops.gates import (
    ACCEPTED_RISKS,
    AUDIT_RECORD,
    AuditFinding,
    AuditRecord,
    GateReport,
    read_audit_record,
)


def record(**kwargs: object) -> AuditRecord:
    base: dict[str, object] = {"recorded_at": "2026-10-12T00:00:00+00:00"}
    base.update(kwargs)
    return AuditRecord(**base)  # type: ignore[arg-type]


class TestAnUnrunAuditIsNotAPass:
    def test_a_missing_record_reports_never_run(self, tmp_path: Path) -> None:
        assert not read_audit_record(root=tmp_path).ran

    def test_a_corrupt_record_reports_never_run(self, tmp_path: Path) -> None:
        (tmp_path / "out").mkdir()
        (tmp_path / AUDIT_RECORD).write_text("{not json", encoding="utf-8")
        assert not read_audit_record(root=tmp_path).ran

    def test_a_non_object_record_reports_never_run(self, tmp_path: Path) -> None:
        (tmp_path / "out").mkdir()
        (tmp_path / AUDIT_RECORD).write_text("[1,2,3]", encoding="utf-8")
        assert not read_audit_record(root=tmp_path).ran

    def test_never_run_fails_the_report(self) -> None:
        """The whole point. An audit nobody ran is not evidence of a clean tree."""
        assert not GateReport(audit=AuditRecord()).ok

    def test_the_summary_says_never_run(self) -> None:
        assert "NEVER RUN" in GateReport(audit=AuditRecord()).summary()

    def test_the_description_says_what_to_do(self) -> None:
        described = AuditRecord().describe()
        assert "--record-audit" in described

    def test_a_recorded_clean_audit_passes(self) -> None:
        assert GateReport(audit=record()).ok


class TestAnUpstreamFixIsNotAnApplyableOne:
    """The distinction my first version got wrong, and it made the gate unsatisfiable."""

    def test_an_advisory_fix_is_reported_as_existing(self) -> None:
        finding = AuditFinding(package="x", version="1", advisory="CVE-1", fix="2")
        assert finding.upstream_fix_exists

    def test_a_finding_with_a_fix_and_no_acceptance_needs_action(self) -> None:
        finding = AuditFinding(package="x", version="1", advisory="CVE-1", fix="2")
        assert finding.needs_action

    def test_an_accepted_finding_does_not_need_action(self) -> None:
        finding = AuditFinding(
            package="fsspec", version="2026.2.0", advisory="CVE-2026-104851", fix="2026.6.0"
        )
        assert finding.accepted_reason
        assert not finding.needs_action

    def test_the_description_states_the_acceptance_reason(self) -> None:
        finding = AuditFinding(
            package="fsspec", version="2026.2.0", advisory="CVE-2026-104851", fix="2026.6.0"
        )
        assert "ACCEPTED" in finding.describe()
        assert "REVIEW WHEN" in finding.describe()

    def test_a_finding_with_no_upstream_fix_does_not_need_action(self) -> None:
        """There is nothing to do, so failing would be a permanently red gate."""
        finding = AuditFinding(package="x", version="1", advisory="CVE-1", fix="")
        assert not finding.needs_action
        assert not finding.accepted_reason

    def test_an_actionable_finding_fails_the_report(self) -> None:
        finding = AuditFinding(package="x", version="1", advisory="CVE-1", fix="2")
        assert not GateReport(audit=record(findings=(finding,))).ok

    def test_an_accepted_finding_does_not_fail_the_report(self) -> None:
        finding = AuditFinding(
            package="fsspec", version="2026.2.0", advisory="CVE-2026-104851", fix="2026.6.0"
        )
        report = GateReport(audit=record(findings=(finding,)))
        assert report.ok
        assert len(report.audit.accepted_findings) == 1

    def test_the_summary_names_actionable_findings_separately(self) -> None:
        finding = AuditFinding(package="x", version="1", advisory="CVE-1", fix="2")
        assert (
            "ACTIONABLE VULNERABILITIES" in GateReport(audit=record(findings=(finding,))).summary()
        )


class TestTheAcceptedRiskTable:
    def test_every_entry_states_a_reason(self) -> None:
        """An acceptance without a reason is a suppression, and a suppression is how a known
        vulnerability becomes an unknown one."""
        for key, reason in ACCEPTED_RISKS.items():
            assert len(reason) > 200, f"{key} is accepted with a thin reason"

    def test_every_entry_says_when_to_review(self) -> None:
        """So the next round that COULD fix it knows to look. Without this, an accepted risk is
        indistinguishable from an ignored one."""
        for key, reason in ACCEPTED_RISKS.items():
            assert "REVIEW WHEN" in reason, f"{key} does not say when to revisit it"

    def test_every_entry_names_the_blocker(self) -> None:
        for key, reason in ACCEPTED_RISKS.items():
            assert (
                "deterministic" in reason.lower()
                or "pin" in reason.lower()
                or "cannot" in reason.lower()
            ), f"{key} does not say why it cannot be acted on"

    def test_the_live_record_is_reported(self) -> None:
        """The recorded audit must actually be READ by the command, not merely written."""
        recorded = read_audit_record()
        assert recorded.ran, "no audit recorded; run `tigerquant gates --record-audit`"
        assert all(isinstance(item.package, str) for item in recorded.findings)


class TestTheRecordRoundTrips:
    def test_it_is_json(self) -> None:
        (Path("out")).mkdir(exist_ok=True)
        original = AUDIT_RECORD.read_text(encoding="utf-8") if AUDIT_RECORD.is_file() else None
        try:
            payload = {
                "recorded_at": "2026-10-12T00:00:00+00:00",
                "detail": "1 finding(s)",
                "findings": [
                    {"package": "p", "version": "1", "advisory": "CVE-1", "fix": "2"},
                ],
            }
            AUDIT_RECORD.write_text(json.dumps(payload), encoding="utf-8")
            parsed = read_audit_record()
            assert parsed.ran
            assert parsed.findings[0].package == "p"
            assert parsed.findings[0].fix == "2"
        finally:
            if original is None:
                AUDIT_RECORD.unlink(missing_ok=True)
            else:
                AUDIT_RECORD.write_text(original, encoding="utf-8")

    @pytest.mark.parametrize("key", sorted(ACCEPTED_RISKS))
    def test_each_accepted_risk_is_a_pair(self, key: tuple[str, str]) -> None:
        assert len(key) == 2
        assert all(isinstance(part, str) and part for part in key)
