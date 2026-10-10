# ADR-0003 — Mode and account isolation

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (implemented incrementally: types in CR-01, Tiger enforcement in CR-02/CR-03)

## Context

AGENTS.md invariants 1, 2, 9 and 10 require that BACKTEST / SHADOW / PAPER / LIVE are not a UI toggle. Acceptance gate 1 requires that a live account refuses all order calls in non-LIVE environments **even when the account ID type is forged**; gate 13 requires paper credentials and event data to be unreachable from live workers.

`config/example.yaml` already carries `app.environment: paper` and `app.allow_live: false`, but a YAML string is not an authorization boundary.

## Decision

1. **`Mode` is a closed enum** `BACKTEST | SHADOW | PAPER | LIVE` and is carried on every event, journal record, order intent and audit entry. Records with mismatched mode are rejected by the journal writer.
2. **Capability set derived from mode, never from config alone.** `ModeCapabilities` is computed from `(mode, deployment_authorization)` where the authorization is a signed, human-issued artifact. `LIVE` requires an explicit authorization object; without it, constructing a live-mode worker raises `LiveNotAuthorized` at startup. Autonomous development never creates one.
3. **Account identity is verified against the broker, not the config.** The Tiger adapter must call the account-info endpoint and assert:
   - the returned account number is the configured one,
   - the reported `account_type`/`capability` matches the required type for the mode (`PAPER` for PAPER, `STANDARD`/`GLOBAL` only for an authorized LIVE deployment),
   - the required type is asserted on **every** submission, not only at startup.
4. **Structural refusal, not policy refusal.** The guard wraps the SDK object rather than being consulted by call sites: `GuardedTradeClient` exposes no write method in BACKTEST/SHADOW, and in PAPER exposes `place_order` only after the identity assertion has succeeded in the current session. Forging `account_type` in config changes nothing because the value is read back from the broker response.
5. **Namespace isolation.** Each mode derives a distinct: order-intent ID prefix, event-namespace/channel name, journal path, database/schema name, secret reference (`secret://tigerquant/<mode>/...`), and worker-lease name. Cross-mode reads are forbidden; a shared helper computes these namespaces from `Mode` alone so a mismatch is a programming error caught by test.
6. **Fake/mock/paper/live labelling.** Every component that touches the outside world declares `ImplementationStatus` ∈ {`SIMULATED`, `PAPER_VERIFIED`, `LIVE_VERIFIED`, `UNAVAILABLE`}, surfaced in the dashboard and in the audit header. `SIMULATED` is the default; nothing may claim `PAPER_VERIFIED` without a recorded paper integration artifact (AGENTS.md invariant 11).

## Consequences

- CR-01 exercises modes BACKTEST/SHADOW only, and its tests assert that PAPER/LIVE capabilities are absent without authorization.
- CR-02 must add a real, recorded account-type verification before any Tiger read is trusted; if paper credentials are unavailable, CR-02/CR-03 stay `BLOCKED` per `docs/WORK_QUEUE.md` and the code paths remain covered by fake-broker contract tests only — explicitly labelled `SIMULATED`.
- A mode switch requires a new process with a new config, a new lease and a new secret reference. There is no in-process transition, so "switch to live at runtime" is not expressible.
