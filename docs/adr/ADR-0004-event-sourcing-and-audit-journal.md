# ADR-0004 — Event sourcing and the append-only audit journal

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (implemented in CR-01)

## Context

PRODUCT_SPEC requires that *every proposed trade can be reconstructed from versioned evidence, model/prompt version, input snapshot, checks, order intent and execution events*, and ARCHITECTURE.md states that the engine's own cache/database **is not** a substitute for an independent audit store. Acceptance gate 15 requires that a database outage produces an alert and no new exposure, and gate 7 requires duplicate events not to double-book fills.

## Decision

1. **The audit journal is the primary record.** It is an append-only JSONL file per mode, one record per line, written with `fsync`-equivalent durability before an action is considered to have happened. Engine caches, broker reports and dashboards are derived caches that can always be rebuilt from the journal.
2. **Envelope + payload split.** Each record is
   ```json
   {"seq": 12, "recorded_at": "...", "mode": "BACKTEST",
    "event_id": "...", "event_type": "OrderFilled", "schema_version": "1.0.0",
    "prev_hash": "sha256:...", "hash": "sha256:...", "payload": { ... }}
   ```
   `hash = sha256(canonical_json(envelope_without_hash))`, chaining `prev_hash`. Canonical JSON is `sort_keys=True`, separators `(",", ":")`, UTF-8, `Decimal` normalised to string, datetimes normalised to UTC ISO-8601 with microsecond precision. Any mutation of any earlier line breaks the chain and is detected by `tigerquant verify-journal`.
3. **Durable identity before side effects.** An `OrderIntent` is journaled with a durable ID *before* any broker call. The state machine in ADR-0008 relies on this ordering.
4. **Idempotent consumption.** Every in-process consumer (ledger, risk state, dashboard projections) is driven by `(event_id)` with an idempotency set, so replaying or duplicating a record is a no-op. Acceptance gate 7 (duplicate callbacks/push events) is a direct test of this property.
5. **Replay is exact.** `tigerquant replay --fixture X --journal out.jsonl` and re-running the same command must produce byte-identical journals, including all IDs and timestamps. This forces determinism in time, IDs and iteration order (ADR-0011) and is the strongest single test of the offline slice.
6. **Journal is not the ledger.** The `PortfolioLedger` is a projection built from filled events; it can be rebuilt at any time and its arithmetic is recomputed deterministically (ADR-0006).

## Consequences

- Determinism becomes a hard engineering constraint (ordered dicts, no `set` iteration in output paths, no `uuid4`, no wall-clock reads outside the injected `Clock`). This is a cost paid once in CR-01 and repaid by every later CR that needs replay evidence.
- Journals grow monotonically; rotation/archival is deferred to CR-09 and must preserve hash continuity across files.
- Storing hashes of prompts and evidence, not their secrets, keeps the journal free of credentials and licensed-content text where the licence forbids redistribution.
