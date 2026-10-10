# TigerQuant repository constitution — read before every change

## Product mission
Build a verifiable AI-directed trader with an established event-driven trading engine, native Tiger paper account support, provider-neutral decision-model integration, evidence-grounded market intelligence, deterministic risk rules, and independently controlled order execution.

## Non-negotiable invariants
1. LIVE trading is OFF by default. No live trades, live credentials, account linking, funding actions, or live activation by coding agents. No tests may place live orders.
2. Paper, shadow, backtest and live configurations MUST use isolated account identities, secrets, workers, order namespaces, databases or strongly enforced data partitions, and audit trails. A mode switch is a deployment authorization process, never an AI tool.
3. Only the authorized execution service may submit orders. AI components have no broker write credentials. Route every proposal through a typed validation and independent risk policy.
4. No evidence means NO_TRADE. Every factual assertion influencing trade admission must reference evidence IDs in a validated point-in-time snapshot. Contradictions or unavailable key inputs produce abstention or a risk freeze, not manufactured certainty.
5. The model must not supply final numerical truth: position size, buying power, costs, mark-to-market, indicators and exposure are recomputed by deterministic services.
6. Provenance: store publication/occurrence/ingestion/availability timestamps, source IDs, content hashes, calculation versions, claim categories, and decision-time snapshots. Backtesting must prevent lookahead and flag training-data leakage.
7. AI outputs, tool arguments, market-data texts, and news content are untrusted. Enforce schemas, evidence resolution, instrument allowlists, prompt-injection resistance and hard risk checks.
8. Every order intent has a durable ID and state machine; never blindly repeat a submission following uncertain API outcomes. Reconcile broker state before any restart permits new exposure.
9. Default universe: US-listed liquid stocks and ETFs, long-only, no margin, no options, no shorting. No unattended live capability until separately authorized.
10. All config changes to live risk or model version require explicit human approval and versioned audit. Never self-escalate permissions, auto-deploy model/prompt changes to live or relax any hard risk limit.
11. DSH must never hide missing functionality with placeholders that appear production-ready. Clearly label mock, paper-tested and verified live behavior.
12. Use official docs + pinned compatible versions; do not guess Tiger/NautilusTrader SDK interfaces. Add API compatibility tests.

## Working method
- Complete one bounded CR at a time (see docs/WORK_QUEUE.md).
- First inspect repository state and installed dependencies. Write design/tests before major implementation.
- Prefer vertical slices with executable acceptance tests rather than horizontal scaffolding.
- Run unit, integration, static analysis, dependency audit and typecheck where relevant.
- After each CR: update docs/PROGRESS.md, write test evidence, list known limitations, commit to the working branch and stop/continue according to configured human approval gates.
- Do not merge to the protected main branch without passing CI and recorded review.
- Resolve conflicting goals in favor of correctness, safety and minimal complexity.
- Maintain a failure-injection test suite from CR-02 onward.

## Security
No keys in Git, logs, screenshots, prompts, model tools or audit outputs. Production secrets belong in a secret manager; tests must use fake keys. Do not use network-exposed DSH with permissive shell access. Restrict DSH to the development workspace and paper/testing services only.
