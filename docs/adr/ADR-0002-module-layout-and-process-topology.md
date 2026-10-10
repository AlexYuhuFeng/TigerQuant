# ADR-0002 — Module layout and process topology

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00
- Supersedes: none

## Context

The product needs a data plane, an AI decision plane and an execution plane with *unbalanced* trust (AGENTS.md invariants 1–3). A single process that both thinks and trades is exactly the arrangement the invariants forbid. At the same time, over-engineering distributed infrastructure in CR-01 would burn the budget without producing a working slice.

## Decision

### 1. Package layout (single repository, single distribution, layered modules)

```
src/tigerquant/
  core/        # clock, ids, money, canonical JSON, enums, errors     (L0)
  events/      # event schemas, registry, append-only hash-chained journal (L0)
  market/      # fixture-driven deterministic feed, as-of point-in-time view (L1)
  evidence/    # fact store, provenance, snapshots, contradiction checks (L1)
  portfolio/   # ledger, deterministic sizing, mark-to-market          (L1)
  risk/        # policy model + gateway (independent authority)        (L1)
  decision/    # typed decision contract, schema+evidence validation   (L1)
  models/      # provider-neutral ModelService + provider implementations (L2)
  execution/   # order intent state machine, broker protocol, fake broker (L2)
  adapters/    # tiger/ (CR-02/03), nautilus/ (CR-04)                  (L3)
  ops/         # watchdog, alerts, supervisor, runbooks hooks          (L2)
  config/      # settings models, mode/entitlement resolution          (L0)
  pipeline/    # wiring: replay runner, paper runner                   (L4)
  dashboard/   # read-only operator surface                            (L4)
  cli.py       # console entry point
```

Layer numbers are enforced by an import-boundary test: an import from a lower layer to a higher one fails CI.

### 2. Process topology

| Process | Mode | Privileges | CR |
|---|---|---|---|
| `replay` (library call, no daemon) | BACKTEST | read fixtures, write journal; no sockets | CR-01 |
| `shadow` worker | SHADOW | market data read; **no broker client constructed** | CR-09 |
| `paper` worker | PAPER | Tiger paper credentials; owns the only `place_order` call site | CR-03 |
| `live` worker | LIVE | *not built, not authorized* | CR-10 doc only |
| dashboard | none | read-only view over the audit journal and state store | CR-08 |

Each mode gets its own **worker lease**, **event namespace**, **database/schema**, and **secret reference**. A mode switch is a deployment action (new process, new config, new lease), never a runtime flag toggled by a model, an API call or the dashboard.

### 3. Trust zoning inside the paper worker

The paper worker is itself split into two asyncio task groups with different object graphs:

- **Decision segment**: market data → evidence → model → validation → sizing → risk → `OrderIntent`. It holds *no broker client object at all*.
- **Execution segment**: `OrderIntent` → gateway re-check → broker adapter → fill/ack events → ledger → reconciliation. It holds the broker client and never receives model text.

The only channel between them is a typed `OrderIntent` value object plus the append-only journal. There is no function reference, no shared mutable state and no credential handle crossing that boundary; a test asserts the decision segment's object graph cannot reach `TradeClient` (acceptance gate 2).

## Consequences

- CR-01 can be built and fully tested with the replay process alone.
- The eventual live deployment is a configuration and authorization exercise on infrastructure that already exists, rather than a rewrite. No code changes are needed to switch modes — only a different, human-approved deployment.
- One extra process boundary costs some plumbing (`OrderIntent` serialisation) and buys the property that the model literally cannot reach order submission.
- Dashboard is deliberately read-only in V1 so that acceptance gate 16 has no unauthenticated write path to protect.
