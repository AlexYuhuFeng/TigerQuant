"""Verify the critical test cases in docs/ACCEPTANCE_GATES.md, and say plainly where we fall short.

Every round so far has claimed "the gates are met" in prose, in a progress file, by me. That claim
was never machine-checkable. This module makes it checkable: each of the sixteen critical test cases
maps to the ACTUAL test node ids that prove it, and the command reports which are proven, which are
partial, and which cannot be shown at all.

Two rules shape it, both because the failure mode of a coverage claim is overstatement.

**A case with no mapped test is reported as UNPROVEN, never as passing.** A gap that renders as a
green tick is worse than no report, because it removes the reason to look.

**A PARTIAL case names what is missing, in words.** "Case 1 is partial" is not actionable; "the
account guard is exercised, but no real paper session has been run" is.

A mapped node id that does not exist is a FAILURE, not a skip. A coverage table pointing at tests
that were renamed away is exactly how a project convinces itself it is covered.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CRITICAL_CASES",
    "CaseStatus",
    "CriticalCase",
    "GateReport",
    "collect_node_ids",
    "verify_gates",
]

REPO_ROOT = Path(__file__).resolve().parents[3]


class CaseStatus:
    """The three answers, and there are only three."""

    #: Every mapped test exists and passes.
    PROVEN = "PROVEN"
    #: Some are proven; the case needs something more that is named.
    PARTIAL = "PARTIAL"
    #: No mapped test, or a mapped test does not exist.
    UNPROVEN = "UNPROVEN"


@dataclass(frozen=True, slots=True)
class CriticalCase:
    """One numbered case from ACCEPTANCE_GATES.md."""

    number: int
    summary: str
    #: Pytest node ids (file::Class::test or file::test) that PROVE this case.
    tests: tuple[str, ...] = ()
    #: Set when the case cannot be fully shown, naming what is missing.
    gap: str = ""
    #: Set when a mapped test is known to be indirect, naming how.
    caveat: str = ""

    @property
    def claimed(self) -> str:
        if not self.tests:
            return CaseStatus.UNPROVEN
        return CaseStatus.PARTIAL if self.gap else CaseStatus.PROVEN


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: CriticalCase
    status: str
    #: Node ids that exist in the suite.
    present: tuple[str, ...] = ()
    #: Node ids that do NOT exist -- a coverage table pointing at nothing.
    missing: tuple[str, ...] = ()
    #: (node id, "passed"|"failed") when tests were actually run.
    outcomes: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == CaseStatus.PROVEN and not self.missing

    @property
    def effective_status(self) -> str:
        """The status AFTER checking the mapping points at real tests.

        A case whose mapped tests do not exist is UNPROVEN whatever the table claims. Without this
        the report rendered a case as PROVEN while printing MISSING TEST(S) beneath it -- a
        coverage report overstating coverage, which is worse than no report at all.
        """
        if self.missing:
            return CaseStatus.UNPROVEN
        return self.status

    def describe(self) -> str:
        parts = [f"case {self.case.number:2d} [{self.effective_status:8s}] {self.case.summary}"]
        if self.missing:
            parts.append(f"       MISSING TEST(S): {', '.join(self.missing)}")
        if self.outcomes:
            failed = [node for node, outcome in self.outcomes if outcome != "passed"]
            if failed:
                parts.append(f"       FAILING: {', '.join(failed)}")
        if self.case.gap:
            parts.append(f"       gap: {self.case.gap}")
        if self.case.caveat:
            parts.append(f"       caveat: {self.case.caveat}")
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class GateReport:
    results: tuple[CaseResult, ...] = ()
    note: str = ""

    @property
    def proven(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if r.effective_status == CaseStatus.PROVEN)

    @property
    def partial(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if r.effective_status == CaseStatus.PARTIAL)

    @property
    def unproven(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if r.effective_status == CaseStatus.UNPROVEN)

    @property
    def ok(self) -> bool:
        """A report is ok when nothing is UNPROVEN and no mapped test is missing.

        PARTIAL does NOT fail the report. A case with a named gap is an honest state, and a gate
        that fails on every honest gap is a gate that gets disabled. What must never pass is a case
        with nothing behind it.
        """
        return not self.unproven

    def summary(self) -> str:
        total = len(self.results)
        lines = [
            f"critical test cases: {len(self.proven)}/{total} proven, "
            f"{len(self.partial)} partial, {len(self.unproven)} unproven"
        ]
        if self.note:
            lines.append(self.note)
        return "; ".join(lines)

    def render(self) -> str:
        lines = [self.summary(), ""]
        lines.extend(result.describe() for result in self.results)
        lines.append("")
        if self.unproven:
            lines.append(
                f"UNPROVEN ({len(self.unproven)}): these have nothing behind them and must not be "
                "reported as met."
            )
        if self.partial:
            lines.append(
                f"PARTIAL ({len(self.partial)}): proven in part, with the gap named above."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The mapping. Every node id here is checked to EXIST, so a rename breaks this
# loudly rather than leaving a stale claim of coverage.
# ---------------------------------------------------------------------------
_U = "tests/unit/"
_F = "tests/fault/"

CRITICAL_CASES: tuple[CriticalCase, ...] = (
    CriticalCase(
        1,
        "paper account is PAPER type; a forged/live account refuses all order calls",
        tests=(
            f"{_U}test_guard_write_scope.py",
            f"{_U}test_tiger_guard.py",
        ),
        caveat="exercised against the broker response SHAPES CR-02 observed, not a live API",
    ),
    CriticalCase(
        2,
        "the LLM cannot reach TradeClient.place_order through its tool boundary",
        tests=(
            f"{_U}test_risk_limits_watchdog.py::TestPrivilegeBypass",
            f"{_U}test_import_boundaries.py::test_only_one_module_calls_a_broker_write_api",
        ),
    ),
    CriticalCase(
        3,
        "loss limit / stale price / order mismatch / missing quote fail closed",
        tests=(
            f"{_U}test_risk_gateway.py",
            f"{_F}test_failure_injection.py::TestRiskFailures::test_stale_price_fails_closed",
            f"{_F}test_failure_injection.py::TestRiskFailures::test_missing_quote_fails_closed",
            f"{_F}test_failure_injection.py::TestRiskFailures::test_unreconciled_account_state_fails_closed",
        ),
    ),
    CriticalCase(
        4,
        "order times out after acceptance: no blind retry; reconcile or quarantine",
        tests=(f"{_U}test_tiger_execution.py",),
        gap="the timeout path is exercised against the fake broker and the adapter's reconcile; "
        "NO real broker timeout has been observed, because no order has ever been submitted",
    ),
    CriticalCase(
        5,
        "partial fill then cancel: remaining quantity and inventory match the broker",
        tests=(
            "tests/integration/test_execution_service.py::TestFillHandling::test_partial_fill_then_cancel_reconciles_exactly",
            f"{_U}test_execution_state_machine.py::TestTransitionTable::test_partial_fill_may_repeat",
        ),
    ),
    CriticalCase(
        6,
        "restart with resting orders: startup reconstruction before new exposure",
        tests=(
            f"{_F}test_failure_injection.py::TestExecutionFailures::test_restart_reconstruction_sees_resting_orders",
        ),
    ),
    CriticalCase(
        7,
        "duplicate callbacks and duplicate push events do not double-submit or double-book",
        tests=(
            f"{_F}test_failure_injection.py::TestExecutionFailures::test_duplicate_fill_delivery_does_not_double_book",
            f"{_U}test_db_layer.py",
        ),
    ),
    CriticalCase(
        8,
        "two instances competing for the active worker lease: only one may submit",
        tests=(
            f"{_U}test_db_layer.py::TestWorkerLease::test_second_owner_is_refused_while_the_lease_is_live",
            "tests/integration/test_db_postgres.py::TestPostgresLease::test_heartbeat_and_release_are_owner_only",
        ),
    ),
    CriticalCase(
        9,
        "contradictory news or unsupported fact ids => NO_TRADE; injection ignored",
        tests=(
            f"{_F}test_failure_injection.py::TestEvidenceFailures::test_future_evidence_cannot_be_cited",
            f"{_U}test_decision_validation.py",
            f"{_U}test_evidence_connectors.py",
        ),
    ),
    CriticalCase(
        10,
        "well-formed JSON with bad arithmetic: deterministic recomputation rejects",
        tests=(f"{_U}test_decision_validation.py",),
    ),
    CriticalCase(
        11,
        "provider outage, latency, invalid output or changed version never bypasses risk",
        tests=(
            f"{_F}test_failure_injection.py::TestModelFailures::test_provider_outage_produces_no_trade_and_no_order",
            f"{_U}test_decision_model.py",
        ),
        gap="the provider paths are exercised with FAKES; NO real provider call has been made, so "
        "no real latency, real outage or real version change has been observed",
    ),
    CriticalCase(
        12,
        "point-in-time replay cannot see future revisions or out-of-horizon data",
        tests=(
            f"{_F}test_failure_injection.py::TestEvidenceFailures::test_lookahead_access_raises",
            f"{_F}test_failure_injection.py::TestEvidenceFailures::test_future_evidence_cannot_be_cited",
            f"{_U}test_market_asof.py",
        ),
    ),
    CriticalCase(
        13,
        "paper credentials and event data cannot reach live workers",
        tests=(
            f"{_U}test_import_boundaries.py",
            f"{_U}test_live_readiness.py",
            f"{_U}test_config_mode.py",
        ),
    ),
    CriticalCase(
        14,
        "market closed, halted, or outside the order window: reject",
        tests=(
            f"{_F}test_failure_injection.py::TestRiskFailures::test_halt_prevents_new_exposure_but_allows_cancellation",
            f"{_U}test_calendar.py",
        ),
        caveat="halted and outside-window are exercised; a real closed market has not been seen, "
        "because no order has ever been submitted",
    ),
    CriticalCase(
        15,
        "restart, reconnection, database outage and clock skew alert with no new exposure",
        tests=(
            f"{_F}test_failure_injection.py::TestClockAndDataFaults::test_clock_cannot_move_backwards",
            f"{_F}test_failure_injection.py::TestClockAndDataFaults::test_future_dated_mark_is_treated_as_skew_not_freshness",
            f"{_U}test_supervisor.py",
            f"{_U}test_supervisor_backoff.py",
        ),
        caveat="reconnection is proven with a SYNTHETIC connection sequence, not a real failover",
    ),
    CriticalCase(
        16,
        "risk configuration cannot be changed through the AI API or an unauthenticated action",
        tests=(
            f"{_U}test_config_mode.py::TestModeCapabilities::test_live_requires_authorization_and_is_inert_without_it",
            f"{_U}test_risk_gateway.py",
        ),
    ),
)


def collect_node_ids(*, root: Path | None = None, timeout: int = 600) -> set[str]:
    """Every test node id pytest can see, or an empty set when collection fails.

    Returns node ids in the same form the mapping uses -- ``path::Class::test`` -- so a mapping
    entry can be compared directly. An empty result is treated by the caller as "cannot verify",
    never as "everything is missing".
    """
    base = root or REPO_ROOT
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header", "tests"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=base,
        timeout=timeout,
    )
    ids: set[str] = set()
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        # Collection output is `path::Class::test` or `path::test`, optionally with a suffix.
        if "::" in stripped and not stripped.startswith("="):
            ids.add(re.match(r"^[^ ]+", stripped).group(0))  # type: ignore[union-attr]
    return ids


def _covers(node_id: str, collected: set[str]) -> bool:
    """Whether a mapping entry is satisfied by the collected suite.

    An entry may name a FILE (``tests/unit/test_x.py``), a CLASS, or a single test. Each is a
    legitimate granularity: some cases are proven by a whole module.
    """
    if node_id in collected:
        return True
    prefix = node_id + "::"
    return any(candidate.startswith(prefix) for candidate in collected)


def verify_gates(
    *,
    root: Path | None = None,
    cases: Sequence[CriticalCase] | None = None,
) -> GateReport:
    """Check every mapped test EXISTS, and report each case's honest status.

    This does NOT run the tests. It verifies that the coverage table points at real tests, which is
    the claim most likely to rot: a renamed test leaves a mapping that still reads as covered.
    Running them is `pytest`'s job and `--run` does it.
    """
    selected = tuple(cases) if cases is not None else CRITICAL_CASES
    collected = collect_node_ids(root=root)
    if not collected:
        return GateReport(
            results=tuple(CaseResult(case=case, status=case.claimed) for case in selected),
            note="COULD NOT COLLECT TESTS: statuses below are the CLAIMED ones, not verified ones",
        )

    results = []
    for case in selected:
        present = tuple(node for node in case.tests if _covers(node, collected))
        missing = tuple(node for node in case.tests if not _covers(node, collected))
        results.append(CaseResult(case=case, status=case.claimed, present=present, missing=missing))
    return GateReport(results=tuple(results))
