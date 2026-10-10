# Component map — TigerQuant (CR-00 baseline)

Every component declares its **trust level**, the **privileges it may hold**, and the **CR that delivers it**. A component may not exceed its declared privileges; this table is the review artifact for "approved component map" in the CR-00 gate.

Trust levels: `T0` pure/deterministic, no I/O · `T1` reads local fixtures/state · `T2` reads external unverified data · `T3` holds a broker write capability.

| Component | Module | Trust | May do | May **not** do | CR |
|---|---|---|---|---|---|
| Core primitives (clock, ids, money, canonical json, enums, errors) | `core/` | T0 | pure computation | read wall-clock outside `SystemClock`; use `uuid4`; use `float` for money | CR-01 |
| Event schemas + registry | `events/` | T0 | define/parse typed events | perform I/O | CR-01 |
| Audit journal (append-only, hash-chained) | `events/journal.py` | T1 | append + verify + replay local journal | rewrite history; accept a mode-mismatched record | CR-01 |
| Fixture loader | `market/fixtures.py` | T1 | read YAML/JSON scenario fixtures | contact a network | CR-01 |
| Deterministic market feed | `market/fake_feed.py` | T1 | emit bars/quotes from fixtures | fabricate data absent from the fixture | CR-01 |
| Point-in-time view (as-of guard) | `market/asof.py` | T0 | expose data with `timestamp <= as_of` | expose future data; be bypassed by indicators | CR-01 |
| Reference strategy (**not an LLM**, labelled) | `strategy/reference.py` | T0 | emit a candidate `TradeDecision` from an as-of view | call a broker; call a model; exceed plan limits | CR-01 |
| Decision contract + validator | `decision/` | T0 | validate against `schemas/trade_decision.schema.json`; resolve evidence; recompute | trust model numbers; approve without evidence | CR-01 (recompute), CR-06 (model) |
| Deterministic sizing | `portfolio/sizing.py` | T0 | convert target weight → integer shares from ledger + mark | accept a model-provided quantity | CR-01 |
| Portfolio ledger | `portfolio/ledger.py` | T0 | positions, cash, realized/unrealized, marks | use `float`; read the broker directly | CR-01 |
| Risk policy + gateway | `risk/` | T0 | the only ALLOW authority; kill switch state | call a model; be bypassed; mutate at runtime | CR-01 (baseline), CR-07 (full) |
| Order intent + state machine | `execution/intent.py` | T0 | validate transitions; mint durable ids | be constructed without a journaled id | CR-01 |
| Fake broker (deterministic) | `execution/fake_broker.py` | T1 | simulate ack/fill/partial/reject/timeout scenarios | be presented as a real broker; accept a raw intent | CR-01 |
| Tiger read-only client | `adapters/tiger/read.py` | T2 | read accounts/assets/positions/orders | submit, modify or cancel anything | CR-02 |
| Tiger account identity guard | `adapters/tiger/guard.py` | T2 | assert broker-reported account type per submission | trust config-supplied account type | CR-02/03 |
| Tiger paper execution adapter | `adapters/tiger/execution.py` | **T3** | the **only** `place_order` call site, `ApprovedOrder` only, PAPER asserted | be imported by decision/strategy/model code; run in BACKTEST/SHADOW/LIVE | CR-03 |
| Reconciler | `execution/reconciler.py` | T2 | query broker, advance/quarantine intents, open the startup gate | submit new orders to resolve an unknown outcome | CR-03 |
| Evidence store + snapshot builder | `evidence/` | T1 | immutable facts, as-of queries, snapshot digest, staleness/contradiction | expose a mutable "latest" read; raise completeness on injection | CR-05 |
| Exchange calendar / session windows | `evidence/calendar.py` | T1 | session, halt, half-day, approved order window | be defaulted from the engine | CR-05 |
| Ingestion connectors | `evidence/sources/` | T2 | fetch + record provenance and digests | be trusted; contain instructions; hold broker credentials | CR-05 |
| Model service (provider-neutral) | `models/` | T2 | one decision completion per request; return text+telemetry | hold a broker handle; expose tools; auto-fallback | CR-06 |
| Provider registry + aliases | `models/registry.py` | T1 | resolve alias → provider/model/settings, versioned | resolve an unapproved alias in paper mode | CR-06 |
| LiteLLM/DeepSeek provider module | `models/providers/` | T2 | HTTP to the model gateway | see evidence text as instructions | CR-06 |
| Watchdog / supervisor | `ops/` | T1 | set halt/freeze, alert, restart supervision | clear a human halt; enable live | CR-07/CR-09 |
| Dashboard (read-only) | `dashboard/` | T1 | render state, evidence lineage, mode badges | write risk config, submit orders, change mode | CR-08 |
| CLI | `cli.py` | varies | expose `replay`, `verify-journal`, `doctor` | expose a live-order command | CR-01 |
| NautilusTrader adapter | `adapters/nautilus/` | T3 (paper) | engine lifecycle, order state, parity with simulator | bypass the gateway; act as the audit store | CR-04 |

## Import boundary rules (enforced by test, not convention)

```
core  <-  events  <-  market/portfolio/risk/decision/evidence  <-  models/execution/ops  <-  adapters  <-  pipeline/cli
```

1. Imports may only point **left** in that chain.
2. `models/`, `decision/`, `strategy/` and `evidence/` may not import `adapters/` or `execution/broker`-write code (gate 2).
3. Only `execution/` and `adapters/` may import a broker SDK; only `adapters/tiger/execution.py` may call a write method (gate 2, T-01).
4. Optional third-party SDKs (`tigeropen`, `nautilus_trader`, `litellm`, FastAPI) are imported lazily and only inside their adapter modules (ADR-0001 §6).

## Process placement

| Component | replay | shadow | paper | live |
|---|---|---|---|---|
| core/events (all) | ✓ | ✓ | ✓ | n/a |
| market fixture feed | ✓ | — | — | — |
| live market data | — | ✓ | ✓ | n/a |
| evidence store | ✓ | ✓ | ✓ | n/a |
| model service | scripted only | ✓ | ✓ | n/a |
| risk gateway | ✓ | ✓ | ✓ | n/a |
| fake broker | ✓ | ✓ (no submit) | — | — |
| Tiger read client | — | — | ✓ | n/a |
| Tiger paper execution | — | — | ✓ | n/a |
| live execution | — | — | — | **not built, not authorized** |
