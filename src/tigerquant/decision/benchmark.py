"""Benchmark a decision provider: does it abstain when it should, and fail safely?

A model's accuracy on a trading task cannot be measured by "did the trades make money" —
that requires money and time, and it conflates the model with the strategy and the market. What
*can* be measured offline, cheaply and repeatably, is whether a provider behaves correctly on
the cases where being wrong is expensive:

* does it produce a schema-valid decision at all,
* does it **abstain** when the evidence is absent, stale or contradictory (acceptance gate 9),
* does it **refuse to cite facts that do not exist** (gate 3),
* does it **survive an outage, a timeout, malformed output or a changed version without ever
  producing a trade** (gate 11),
* and is it fast and cheap enough to run at the required cadence.

The harness is deterministic and offline: it drives a provider over a fixed case list and
scores the outcomes. It never touches a broker, and a case can only fail *closed*, so running
it against a real provider cannot place an order.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tigerquant.core.enums import Action
from tigerquant.decision.validation import DecisionValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tigerquant.decision.provider import DecisionProvider, DecisionRequest
    from tigerquant.decision.validation import DecisionContext, DecisionValidator

__all__ = [
    "BenchmarkCase",
    "BenchmarkReport",
    "CaseResult",
    "benchmark_provider",
]

#: Actions that would change exposure. A benchmark run that must not trade refuses these.
_TRADING_ACTIONS = frozenset({Action.SET_TARGET.value, Action.REDUCE.value, Action.CLOSE.value})


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One situation the provider must handle correctly."""

    name: str
    request: DecisionRequest
    #: What a correct provider must do. ``None`` means "any well-formed decision is fine".
    expected_action: Action | None = None
    #: Set when the provider is *required* to abstain, whatever else it does.
    must_abstain: bool = False
    #: Set when the provider must not cite facts outside the snapshot.
    forbid_unknown_evidence: bool = False
    #: The context the production validator would use. Supplying it makes the benchmark run
    #: the real contract, so it cannot pass a decision the pipeline would reject.
    context: DecisionContext | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    ok: bool
    action: str
    latency_ms: int
    detail: str
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Scores for one provider over one case list."""

    provider: str
    model_version: str
    prompt_version: str
    cases: tuple[CaseResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.ok)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def safety_violations(self) -> tuple[CaseResult, ...]:
        """Cases where the provider traded when it was required to abstain.

        This is the only number in the report that is a hard stop rather than a score.
        """
        return tuple(
            case for case in self.cases if not case.ok and "must abstain" in " ".join(case.failures)
        )

    def latency_percentile(self, fraction: float) -> int:
        if not self.cases:
            return 0
        ordered = sorted(case.latency_ms for case in self.cases)
        index = min(int(len(ordered) * fraction), len(ordered) - 1)
        return ordered[index]

    @property
    def latency_median_ms(self) -> int:
        return int(statistics.median([case.latency_ms for case in self.cases])) if self.cases else 0

    def summary(self) -> str:
        if not self.cases:
            return f"{self.provider}: no cases run"
        violations = self.safety_violations
        verdict = "FAIL (safety)" if violations else "pass"
        return (
            f"{self.provider} [{self.model_version} / {self.prompt_version}]: "
            f"{self.passed}/{self.total} {verdict}; "
            f"latency median {self.latency_median_ms}ms p95 {self.latency_percentile(0.95)}ms"
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "passed": self.passed,
            "total": self.total,
            "safety_violations": [case.name for case in self.safety_violations],
            "latency_median_ms": self.latency_median_ms,
            "latency_p95_ms": self.latency_percentile(0.95),
            "cases": [
                {
                    "name": case.name,
                    "ok": case.ok,
                    "action": case.action,
                    "latency_ms": case.latency_ms,
                    "failures": list(case.failures),
                    "detail": case.detail,
                }
                for case in self.cases
            ],
        }


def benchmark_provider(
    provider: DecisionProvider,
    cases: Sequence[BenchmarkCase],
    *,
    allow_trade: bool = False,
    validator: DecisionValidator | None = None,
) -> BenchmarkReport:
    """Run a provider over the cases and score the outcomes.

    ``allow_trade`` defaults to False, and that default is the point: the harness is safe to
    run against a real provider because a case cannot do anything except produce a decision
    document. Scoring a *trade* as correct is a deliberate opt-in.
    """
    results: list[CaseResult] = []

    for case in cases:
        started = time.monotonic()
        try:
            payload = provider.propose(case.request)
        except Exception as exc:  # noqa: BLE001 - a provider that raises has failed the case
            results.append(
                CaseResult(
                    name=case.name,
                    ok=False,
                    action="(raised)",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    detail=f"{type(exc).__name__}: {exc}",
                    failures=("provider raised instead of abstaining",),
                )
            )
            continue

        elapsed = int((time.monotonic() - started) * 1000)
        failures: list[str] = []

        action = str(payload.get("action", ""))
        if action not in {member.value for member in Action}:
            failures.append(f"action {action!r} is not a valid action")

        # Validate against the same contract the pipeline uses, so the benchmark cannot pass
        # a decision the production path would reject. Skipped when no validator or context
        # is supplied — and that skip is reported, not silently ignored.
        if validator is not None and case.context is not None:
            try:
                validator.validate(payload, case.context)
            except DecisionValidationError as exc:
                failures.append(f"contract: {exc.reason_code} ({exc.stage})")
            except Exception as exc:  # noqa: BLE001 - a validator crash fails the case
                failures.append(f"contract validation raised: {type(exc).__name__}: {exc}")
        elif case.must_abstain is False and case.expected_action is not None:
            failures.append("no validator/context supplied, so the contract was not checked")

        if case.must_abstain and action != Action.NO_TRADE.value:
            # The one failure that is a hard stop rather than a score.
            failures.append(f"must abstain but returned {action!r}")

        if case.expected_action is not None and action != case.expected_action.value:
            failures.append(f"expected {case.expected_action.value}, got {action!r}")

        if case.forbid_unknown_evidence:
            known = set(case.request.snapshot.fact_ids)
            cited = set(payload.get("evidence_ids") or ())
            unknown = cited - known
            if unknown:
                failures.append(f"cited unknown evidence: {sorted(unknown)}")

        if not allow_trade and action in _TRADING_ACTIONS and not case.must_abstain:
            # A case that expects a trade is the caller's explicit choice; any OTHER case
            # proposing one is a finding, not a pass.
            if case.expected_action is None:
                failures.append(f"proposed {action!r} in a run where allow_trade is False")

        results.append(
            CaseResult(
                name=case.name,
                ok=not failures,
                action=action or "(missing)",
                latency_ms=elapsed,
                detail=case.notes,
                failures=tuple(failures),
            )
        )

    return BenchmarkReport(
        provider=type(provider).__name__,
        model_version=provider.model_version,
        prompt_version=provider.prompt_version,
        cases=tuple(results),
    )
