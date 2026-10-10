# ADR-0006 — Decision contract and deterministic recomputation

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (contract validation in CR-01; model integration in CR-06)

## Context

`schemas/trade_decision.schema.json` already defines the untrusted AI proposal shape. Invariant 5 says the model must not supply final numerical truth. Acceptance gate 10 requires that well-formed JSON with wrong arithmetic be rejected by deterministic recomputation. Gate 9 requires unsupported/contradictory evidence IDs to yield `NO_TRADE`.

## Decision

1. **Two-stage validation, in this order.**
   - **Stage A — structural.** `jsonschema` validation against the *repository file* `schemas/trade_decision.schema.json` (draft 2020-12), loaded at runtime rather than re-declared in Python, so the contract in the repository is the single source of truth. Additional semantic checks on top: `action`-dependent field presence, instrument allowlist membership, `target_weight` bounds, non-empty thesis/counter-thesis/invalidation for any action that is not `NO_TRADE`.
   - **Stage B — semantic.** Evidence resolution (every referenced ID must resolve inside the snapshot being acted on), contradiction check, staleness check, and arithmetic recomputation.
2. **The model may state a target weight; it may not state quantity, price, notional, buying power, cost, or P&L.** Those are computed by `portfolio.sizing` and `portfolio.ledger` from the ledger and the latest admissible mark. Any numeric field the model supplies that is not in the schema is rejected by `additionalProperties: false`.
3. **Recomputation is authoritative.** Where a model output contains a claim of arithmetic (e.g. an implied dollar notional inside `thesis`), the value is treated as narrative only; the executable decision uses recomputed numbers. Gate 10's test asserts that a decision whose stated numbers disagree with recomputation is downgraded to `NO_TRADE` and flagged.
4. **`NO_TRADE` is a first-class success.** An empty/absent decision, a provider failure, a timeout, a low-confidence model answer, or any failed check produces `NO_TRADE` with a machine-readable reason code. There is no path where a validation failure becomes "trade anyway with defaults".
5. **Decision IDs and snapshots are pinned.** `decision_id` is allocated by us, not the model, and is `decision:<mode>:<correlation>:<seq>`. `snapshot_id` must equal the digest of the snapshot actually presented; a mismatch is rejected.
6. **Model version pinning.** `model_version` must match the resolved provider release recorded by the model service, not a self-declared alias. A changed resolved version invalidates the decision record for promotion purposes (ARCHITECTURE.md "Model changes").

## Consequences

- The model's only real power is to choose `action`, `instrument`, `target_weight` and to justify it. This is intentionally a narrow, auditable channel.
- Stage A running against the checked-in schema means a schema change is a reviewed, versioned repository event, not a silent prompt change.
- Deterministic recomputation requires the ledger mark to be reproducible; the mark's source is recorded in the snapshot, and a stale mark fails closed (gate 3).
