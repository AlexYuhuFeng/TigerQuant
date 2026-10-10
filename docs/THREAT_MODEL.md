# Threat model — TigerQuant

- Version: 1.0 (CR-00)
- Date: 2026-10-10
- Method: asset-centric, STRIDE-informed, mapped to `docs/ACCEPTANCE_GATES.md`
- Scope: BACKTEST, SHADOW and PAPER operation during autonomous development. LIVE is out of scope and unauthorized (AGENTS.md invariant 1).

## 1. Assets

| ID | Asset | Why it matters | Where it lives |
|---|---|---|---|
| A1 | Broker write capability | Placing an unintended order is the only way this system can lose money | PAPER worker, execution segment only |
| A2 | Broker credentials | Theft enables trading outside our controls | secret manager / env, never in Git, never in a prompt |
| A3 | Audit journal | Sole evidence of what happened; must be tamper-evident | append-only JSONL per mode |
| A4 | Evidence store | Grounds every factual claim; corruption manufactures false confidence | CR-05 store |
| A5 | Risk policy | The limits that bound loss | versioned, human-approved file |
| A6 | Decision records & snapshots | Needed to reconstruct and review any trade | journal + snapshot store |
| A7 | Model API credentials | Cost and abuse; leaking them is an incident | secret manager |
| A8 | Owner authority (mode switch, limit change, live activation) | The last human control | human process, not code |
| A9 | Licensed market data | Redistribution may breach entitlements | fact store with `license_scope` |

## 2. Trust boundaries

```
[ untrusted ]                                        [ trusted only after checks ]
  news / filings / web text ─┐
  broker push payloads ──────┤
  LLM output ────────────────┼──► B1 validation ──► B2 evidence resolution ──► B3 risk gateway ──► B4 execution adapter
  LLM tool arguments ────────┤        (schema)        (as-of, IDs, staleness)     (deny-default)     (ApprovedOrder only)
  config file ───────────────┘
                                                    B5 journal writer (tamper-evident, mode-checked)
                                                    B6 account identity guard (broker-read-back, per submission)
```

- **B1** structural validation (jsonschema + semantic checks).
- **B2** evidence resolution and point-in-time admissibility.
- **B3** the only ALLOW authority.
- **B4** the only broker write path.
- **B5** append-only hash-chained journal.
- **B6** broker-reported account type assertion, re-checked on every submission.

## 3. Threats and mitigations

| ID | Threat | STRIDE | Mitigation | Gate |
|---|---|---|---|---|
| T-01 | LLM coerces an order directly | Elevation | No tool layer; no broker object in the decision segment's graph; import-boundary test; single write call site | 2 |
| T-02 | Prompt injection in news/filings | Tampering | Content is data, never instructions; delimited untrusted blocks; injection detection quality flag; a detected injection cannot raise snapshot completeness; worst case is an invalid proposal | 9 |
| T-03 | Forged/incorrect account type reaches a live account | Spoofing | Account type read back from the broker API and asserted per submission; guard wraps the SDK; structural (method absent) refusal outside PAPER | 1, 13 |
| T-04 | Model supplies plausible but wrong arithmetic | Tampering | Deterministic recomputation of size/cost/exposure; staged validation; disagreement → `NO_TRADE` | 10 |
| T-05 | Stale or missing market data opens risk | Denial of service | `valid_until` staleness gates; missing quote → DENY; fail-closed default | 3 |
| T-06 | Timeout after broker acceptance causes a duplicate order | Repudiation | `SUBMIT_UNKNOWN` state, no blind retry, reconciliation or quarantine, deterministic client order id | 4 |
| T-07 | Duplicate strategy callback / duplicate push double-submits or double-books | Tampering | Idempotency by `event_id` and `(broker_order_id, exec_id)`; durable intent ID | 7 |
| T-08 | Two workers both trade the same account | Elevation | Mode worker lease with owner identity + expiry; only the lease holder may submit | 8 |
| T-09 | Restart with resting orders opens new exposure | Tampering | Startup reconciliation gate must be OPEN before any new intent leaves CREATED | 6 |
| T-10 | Partial fill then cancel leaves wrong inventory | Tampering | Broker-authoritative reconciliation of remaining qty and realized inventory | 5 |
| T-11 | Provider outage/latency/version change degrades safety | Denial of service | Risk and reconciliation run independently of the model; no automatic fallback; NO_TRADE | 11 |
| T-12 | Historical replay sees future revisions or datasets | Information disclosure | `available_at` semantics, as-of queries only, snapshot digest, no mutable reads in replay | 12 |
| T-13 | Paper credentials/events leak into live workers | Information disclosure | Distinct secret refs, namespaces, leases, DBs per mode; cross-mode read forbidden by construction | 13 |
| T-14 | Trading outside market hours/halts | Tampering | Explicit exchange calendar and approved order window; deny unless a documented compatible policy applies | 14 |
| T-15 | Restart/reconnect/DB outage/clock skew silently changes state | Repudiation | Every such condition raises an alert and blocks new exposure; negative time deltas alerted not clamped | 15 |
| T-16 | Risk limits changed via AI API or unauthenticated dashboard | Elevation | Policy loaded once at startup from a versioned file with a recorded hash; dashboard is read-only in V1; no runtime mutation API | 16 |
| T-17 | Journal tampering / silent gap | Repudiation | Hash chain with `prev_hash`; `verify-journal` detects mutation and gaps; mode field checked on write | — |
| T-18 | Secret material leaks into Git, logs, prompts or audit output | Information disclosure | Secret scanning in CI; redaction filter on structured logging; tests use fake keys; `secret://` references only | — |
| T-19 | Supply-chain compromise via floating dependency | Tampering | Exact pins + hash-carrying lockfile; dependency audit job; no floating production dependencies | — |
| T-20 | Lookahead via indicator computed on the full series | Tampering | Indicators are computed from the as-of view object, which raises on any access to a timestamp after the view's `as_of` | 12 |
| T-21 | Fake success: simulated behaviour presented as verified | Repudiation | `ImplementationStatus` labelling (`SIMULATED`/`PAPER_VERIFIED`/`LIVE_VERIFIED`/`UNAVAILABLE`) surfaced in dashboard and audit header | — |
| T-22 | Unbounded resource use / runaway order loop | Denial of service | Order-rate and turnover limits in the risk policy; per-session intent cap; kill switch | 14, 16 |

## 4. Explicitly out of scope (documented, not mitigated here)

- Live/real-money operation, funding, margin, options, shorting — prohibited by AGENTS.md.
- Broker-side compromise and exchange outages beyond the alert/reconcile behaviour covered by T-15.
- Physical/endpoint compromise of the developer workstation.
- Model-training-time leakage (a contemporary model may know historical outcomes); ARCHITECTURE.md requires this be *labelled* rather than "fixed", and forward paper results are the only unbiased evidence.

## 5. Residual risks accepted for CR-01

| Risk | Why accepted | Review point |
|---|---|---|
| In-memory evidence store is not durable | CR-01 is an offline slice; interface fixed in ADR-0005 | CR-05 |
| Rate limiting not yet implemented against Tiger | No Tiger connectivity in CR-01 | CR-03 |
| Journal rotation not implemented | Journals are small in CR-01 | CR-09 |
| Dashboard not built | Read-only by design; no write surface exists yet | CR-08 |
