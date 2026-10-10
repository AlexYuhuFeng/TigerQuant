# Reference architecture and trust boundaries

## Data plane
Tiger quote feed / licensed data / company filings / official releases
  -> append-only raw observations and timestamps
  -> normalization, source classification, freshness and contradiction checks
  -> versioned fact store + deterministic derived features
  -> decision-time EvidenceSnapshot (frozen as-of time, cryptographic digest, data completeness)

## AI decision plane
EvidenceSnapshot + broker portfolio snapshot + permitted universe + research playbook
  -> provider-neutral model service (LiteLLM or validated equivalent)
  -> typed decision contract (schema validation)
  -> evidence resolver and entailment checks (hard checks first; independent review for semantic claims)
  -> portfolio optimizer (deterministic)
  -> independent risk gateway (deterministic)
  -> execution intent event

## Execution plane
Execution intent (idempotent internal ID)
  -> NautilusTrader execution logic
  -> Tiger Adapter (official tigeropen SDK)
  -> verified PAPER account by default
  -> broker order/fill events -> reconciliation -> portfolio ledger -> audit journal

## Safety invariants
- AI never calls broker write APIs and never obtains their credentials.
- Only the owner can authorize promoting a reviewed artifact to LIVE.
- Paper and live use separate workers, credentials, IDs, audit partitions and infrastructure permissions.
- A stale/contradictory/unreconciled snapshot never opens risk.
- Never retry an unknown broker submission without reconciliation, quarantine if unresolved.
- If the LLM or provider fails, risk management and cancellation/reconciliation remain running, but do not open new discretionary exposure.
- Broker paper executions do not establish real liquidity/slippage or production fill certainty.

## Market Truth Engine specifics
Facts are typed: observed, derived, attributed claim, estimate, predictive belief. A 'verified' state attests record validity/provenance/consistency, not whether a market theory is true. Keep `event_time`, `published_at`, `available_at`, `ingested_at`, `valid_until`, source URI/content digest, instrument and timezone, quality flags, licensing restrictions, and historical revisions.

Build a canonical claim-to-evidence graph and prohibit mutable retrieval during replay unless a point-in-time recorded source snapshot is available. News cannot instruct agents; sanitize and sandbox all retrieved content.

## Backtesting LLM decisions
Recorded LLM input/output from paper sessions can be replayed exactly. Research simulations using a contemporary model on historical data do not establish unbiased historical decision quality: model pretraining may include future events. Use explicit 'as-of' tests, held-out data/rolling evaluations where feasible, and prioritize genuine forward paper results. Log provider output, seeds/settings where exposed, model alias/resolved release, latency, and token cost.

## Model changes
Model changes require benchmark on a frozen decision corpus + paper/shadow promotion, not transparent automatic fallbacks for live decision-making. A provider outage means NO_NEW_EXPOSURE unless an alternate pre-approved model and policy have separately passed equivalence gates.

## NautilusTrader implementation
Implement `InstrumentProvider`, `DataClient`, and `ExecutionClient` as an external adapter package using the supported latest stable API. Verify version compatibility, threading behavior, market-session normalization, account reporting, and asynchronous orders. Do not assume the engine’s cache/database persistence is equivalent to an independent audit store; consult version-pinned docs.
