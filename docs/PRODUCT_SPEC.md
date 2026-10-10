# Product Requirements — TigerQuant V2

## Outcome
A continuously operating AI-directed trading system with testable decision quality, not merely a chatbot that can place an order. It must conduct market research on verified inputs; propose evidence-grounded portfolio targets; trade US equities and ETFs through native Tiger paper accounts; backtest through the same decision contract; operate under deterministic risk limits; and be auditable/recoverable when unattended.

## User roles
- Owner: authorizes instrument universe, risk policies, strategy versions and eventually live deployment.
- Trading agent: forms bounded hypotheses and trade proposals using snapshots; no credential or privilege management.
- Trading infrastructure: validates proposals, enforces risk and executes/reconciles brokerage orders.
- Supervisor/watchdog: independently halts new exposure, notifies owner, and monitors the system.

## Modes
1. BACKTEST: as-of data, simulation and fake fills only.
2. SHADOW: live market input, hypothetical orders, no broker submission.
3. PAPER: real Tiger account typed PAPER, actual Tiger paper order workflow with virtual balances.
4. LIVE: real Tiger account, unauthorized by default and outside autonomous development scope.
Mode selection must not be writable by LLM tool calls. No copying outstanding orders or ledger state across modes. Run paper and shadow simultaneously if useful.

## Core functions
- Tiger account discovery and verified account type (PAPER / STANDARD / GLOBAL), read-only cash/positions/order queries; paper order submit/cancel via authorized gateway.
- As-of evidence ingestion: licensed price feed, exchange calendar, brokerage positions, official filings, credible news; record provenance and quality.
- Derived features: deterministic math and data lineage, consistent time zone and corporate actions.
- LLM adapter: start with DeepSeek V4.1 Flash model ID `deepseek-flash` but abstract model/provider via model aliases; other providers later without changing strategy interfaces.
- Decisions: no-trade, hold, reduce, close, or bounded target; structured schema, evidence IDs, contraindications and invalidation conditions.
- Risk engine: position/gross/day-loss/drawdown/order size/turnover/market-data staleness/account reconciliation gates, separate from strategies.
- UI: professional operator dashboard with account mode, positions, orders, current risk, evidence snapshots, AI decision records, alerts, historical tests and paper/live comparison.
- Resilience: supervised process restarts, ambiguity quarantine, startup reconciliation, append-only events, emergency stop, alert routing.

## Non-goals for V1
- Latency-sensitive high-frequency trading, unrestricted leverage, derivatives, self-modifying live code, auto-funding, model-driven changes to risk limits, guaranteed positive returns, automatic live enablement.

## Key operational quality targets (engineering targets, not guaranteed)
- 100% of broker write paths through risk gateway.
- Zero duplicate broker order submissions in deterministic timeout/restart simulations, or explicit quarantine of outcome-unknown intents.
- Every proposed trade can be reconstructed from versioned evidence, model/prompt version, input snapshot, checks, order intent and execution events.
- 100% of model factual evidence IDs resolved or rejected; numeric calculations independently reproduced.
- Paper mode cannot send an order to a live account even with hostile inputs or misconfigured environment.
