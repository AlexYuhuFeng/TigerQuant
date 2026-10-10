# ADR-0008 — Order intent identity and state machine

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (state machine implemented in CR-01, hammered in CR-03)

## Context

Invariant 8: *Every order intent has a durable ID and state machine; never blindly repeat a submission following uncertain API outcomes. Reconcile broker state before any restart permits new exposure.* Acceptance gates 4, 5, 6 and 7 test exactly this.

## Decision

1. **Durable ID before the network.** `OrderIntentId` is minted and journaled *before* any broker call. The ID is derived deterministically (ADR-0011) from `(mode, strategy, correlation, sequence)` so that a crash-restart re-derives the same ID for the same logical intent and cannot create a second one. The broker-side client order id is derived from it and is stable across retries-by-reconciliation.
2. **Explicit states.**
   ```
   CREATED -> RISK_APPROVED -> SUBMITTING -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
                    |              |              |                |
                    v              v              v                v
                 REJECTED   SUBMIT_UNKNOWN   CANCEL_PENDING   CANCELED
                                   |
                                   v
                              QUARANTINED   (terminal until reconciled by a human or by reconciliation)
   ```
   Transitions are validated by an explicit table. An illegal transition raises `IllegalOrderTransition` and journals a `OpsAlert`; it never silently mutates.
3. **`SUBMIT_UNKNOWN` is a real state, not an error.** A timeout or transport error after the request may have been accepted leaves the intent in `SUBMIT_UNKNOWN`. **No automatic resubmission is ever performed from this state.** The only legal exits are (a) reconciliation finds the order at the broker → move to its real state, or (b) reconciliation cannot resolve it within the deadline → `QUARANTINED`, new exposure blocked, owner alerted.
4. **Restart reconciliation precedes exposure.** On worker start, before any new intent may leave `CREATED`, the reconciler must load all non-terminal intents from the journal, query the broker for each, and either advance them or quarantine them. A `StartupGate` object must be `OPEN` before the pipeline can emit an approved order; a test asserts the pipeline is inert while the gate is `CLOSED`.
5. **Fills are idempotent.** Broker fill events are keyed by `(broker_order_id, exec_id)`; a duplicate is dropped by the projection, so inventory and realized P&L cannot be double-booked (gate 7).
6. **Partial fill then cancel** must leave remaining quantity and realized inventory equal to the broker's records; the fake broker implements partial fills in CR-01 so the property is tested offline, and CR-03 re-tests it against Tiger paper (gate 5).
7. **Worker lease.** Only the holder of the mode's lease may move an intent out of `CREATED`. The lease is a file-lock/DB-row with an owner identity and an expiry; a test starts two workers and asserts exactly one can submit (gate 8).

## Consequences

- The state machine is deliberately conservative: it prefers a stuck-but-safe `QUARANTINED` intent over a possible duplicate order.
- Because ids are deterministic, the fake broker in CR-01 can simulate "the request was delivered but the response was lost" and the reconciler path is genuinely exercised rather than mocked.
