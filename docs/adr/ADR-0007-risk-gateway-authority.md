# ADR-0007 — The risk gateway is an independent authority

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (baseline gate in CR-01; full policy/watchdog in CR-07)

## Context

Product target: *100% of broker write paths through risk gateway*; ARCHITECTURE: risk gateway is deterministic and independent of strategies. Acceptance gates 3, 11, 14, 16 require fail-closed behaviour, provider-outage immunity, market-window enforcement, and that risk configuration cannot be changed through the AI decision API or an unauthenticated dashboard action.

## Decision

1. **Single choke point.** `RiskGateway.evaluate(intent, context) -> RiskOutcome` is the only function that can produce an `ApprovedOrder`. The broker adapter's `submit` accepts `ApprovedOrder` and nothing else — the type system, not a convention, enforces the invariant. `OrderIntent` is not submittable.
2. **Fail-closed by construction.** The gateway returns ALLOW only if *every* check returns ALLOW. Any exception inside a check is caught and converted to a DENY with `reason_code=check_error:<name>`; the default branch is DENY. There is no `try: ... except: allow`.
3. **Deny-by-default inputs.** Unknown symbol, symbol outside the versioned allowlist, market closed/halted/outside the approved order window, stale or missing mark, unreconciled account state, missing evidence snapshot, mode capability absent, or a stale risk configuration hash all DENY.
4. **Deterministic and recomputable.** Limits are plain data (`RiskPolicy`, frozen pydantic model) with a content hash recorded in every outcome. A decision is reproducible from `(intent, portfolio snapshot, policy hash, config hash)`.
5. **Independent of the LLM.** The gateway never calls a model. A provider outage (gate 11) does not affect evaluation. Conversely, no model output can raise a limit: the policy object is immutable at runtime and is loaded once at worker start from a human-approved, versioned file.
6. **Kill switch.** A separate `RiskState` (halted / frozen) can be set by the watchdog and by the owner only. A halt blocks new exposure immediately, is journaled, and is sticky across restarts until explicitly cleared by a human. Cancellation and reconciliation continue to run while halted (invariant: risk controls stay alive when the model dies).
7. **No alternate path.** A test enumerates every module that may call a broker write API (allowlist of exactly one adapter file) and fails if a new call site appears. A second test drives the fake broker directly and asserts that submission without an `ApprovedOrder` is impossible.

## Consequences

- Because the gateway is deterministic and pure, CR-01 can implement a meaningful subset (allowlist, long-only, max order fraction, gross exposure, market window, staleness) and CR-07 extends the policy without changing the interface.
- Risk configuration changes are file/deployment events with hashes recorded in evidence — matching invariant 10 and gate 16 (no runtime mutation through any API).
