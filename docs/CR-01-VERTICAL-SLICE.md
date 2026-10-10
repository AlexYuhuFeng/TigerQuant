# CR-01 — Offline vertical slice: specification and verification plan

- Gate: *Reproducible pytest/typecheck/lint in CI; end-to-end synthetic replay; no network/broker required.*
- Status: specification (written before implementation, per AGENTS.md working method)
- Depends on: CR-00 (complete)

## 1. What "vertical slice" means here

A single command must take a **scenario fixture** and run the *entire* decision-to-execution path offline, producing an audit journal that can be verified and replayed byte-for-byte:

```
fixture YAML
  -> deterministic market feed (bars)
    -> point-in-time as-of view            (no lookahead)
      -> reference strategy                 (NOT an LLM; deterministic, labelled SIMULATED)
        -> TradeDecision (JSON)             (validated against schemas/trade_decision.schema.json)
          -> evidence resolution            (every evidence_id must resolve in the snapshot)
            -> deterministic sizing         (target weight -> integer shares)
              -> risk gateway               (deny-by-default; the only ALLOW authority)
                -> OrderIntent              (durable deterministic id, state machine)
                  -> execution service      (paper/shadow/backtest capability check)
                    -> deterministic fake broker (ack / fill / partial / reject / timeout scenarios)
                      -> portfolio ledger   (positions, cash, realized P&L; Decimal only)
                        -> append-only hash-chained journal + replay report
```

Every arrow is a real typed boundary. Nothing in this path is a stub, and every module that is not yet production-grade is labelled with its `ImplementationStatus` and the CR that completes it.

## 2. Deliberate non-goals for CR-01

| Not in CR-01 | CR |
|---|---|
| Real LLM calls, DeepSeek/LiteLLM | CR-06 |
| Tiger connectivity of any kind | CR-02/03 |
| NautilusTrader engine | CR-04 |
| Persistent evidence store, real news/filings ingestion | CR-05 |
| Full risk policy set, watchdog process, kill-switch UI | CR-07 |
| Dashboard | CR-08 |
| Live anything | never (CR-10 is documentation only) |

The CR-01 risk gate implements a **real but partial** policy (allowlist, long-only, gross exposure, per-symbol cap, per-order cap, market window, mark staleness, kill switch state). It is labelled `PARTIAL — CR-07 completes the policy set`, never as complete.

## 3. Deliverables

| # | Deliverable | Path |
|---|---|---|
| D1 | Pinned project definition | `pyproject.toml` |
| D2 | Hash-carrying lockfile | `uv.lock` |
| D3 | Lint / type / test configuration | `pyproject.toml` sections + `tests/` |
| D4 | Core primitives | `src/tigerquant/core/` |
| D5 | Event schemas + registry | `src/tigerquant/events/` |
| D6 | Hash-chained append-only journal | `src/tigerquant/events/journal.py` |
| D7 | Fixture format + deterministic feed + as-of view | `src/tigerquant/market/` |
| D8 | Decision contract + validator | `src/tigerquant/decision/` |
| D9 | Deterministic sizing + portfolio ledger | `src/tigerquant/portfolio/` |
| D10 | Risk policy + gateway + kill switch | `src/tigerquant/risk/` |
| D11 | Order intent state machine + fake broker + execution service | `src/tigerquant/execution/` |
| D12 | Reference strategy (simulated, labelled) | `src/tigerquant/strategy/reference.py` |
| D13 | Minimal evidence store + snapshot (interface frozen for CR-05) | `src/tigerquant/evidence/` |
| D14 | Pipeline wiring + `tigerquant` CLI (`replay`, `verify-journal`, `doctor`) | `src/tigerquant/pipeline/`, `src/tigerquant/cli.py` |
| D15 | Scenario fixtures | `tests/fixtures/scenarios/*.yaml` |
| D16 | Test suite (unit + integration + determinism + failure injection + no-network) | `tests/` |
| D17 | CI workflow (fast lane + optional-integration lane) | `.github/workflows/ci.yml` |
| D18 | Evidence + progress records | `docs/evidence/cr01_*.txt`, `docs/PROGRESS.md` |

## 4. Event schema (v1.0.0)

Every event is a pydantic v2 model with a `schema_version` literal and a common envelope:

`seq:int`, `event_id:str`, `event_type:str`, `schema_version:str`, `mode:Mode`, `occurred_at:datetime`, `recorded_at:datetime`, `correlation_id:str`, `causation_id:str|null`

| Event | Key payload fields |
|---|---|
| `MarketBar` | `instrument`, `interval`, `open/high/low/close: str(Decimal)`, `volume:int`, `available_at`, `event_time`, `source_id`, `content_hash` |
| `MarketQuote` | `instrument`, `bid`, `ask`, `available_at`, `source_id` |
| `SnapshotCreated` | `snapshot_id`, `as_of`, `fact_ids[]`, `completeness`, `digest` |
| `DecisionProposed` | `decision_id`, `snapshot_id`, `model_version`, `action`, `instrument`, `target_weight`, `evidence_ids[]` |
| `DecisionRejected` | `decision_id`, `stage`, `reason_code`, `detail` |
| `RiskEvaluated` | `intent_id`, `outcome` (ALLOW/DENY), `reason_codes[]`, `policy_hash`, `portfolio_digest` |
| `OrderIntentCreated` | `intent_id`, `instrument`, `side`, `quantity:int`, `order_type`, `limit_price|null`, `client_order_id` |
| `OrderSubmitted` / `OrderAcknowledged` / `OrderRejected` / `OrderCanceled` | intent linkage + broker fields |
| `OrderSubmissionUnknown` | `intent_id`, `reason_code`, `attempt` |
| `OrderQuarantined` | `intent_id`, `reason_code` |
| `OrderPartiallyFilled` / `OrderFilled` | `exec_id`, `quantity`, `price`, `commission`, `remaining` |
| `PositionChanged` | `instrument`, `quantity`, `average_cost`, `realized_pnl` |
| `LedgerMarked` | `as_of`, `cash`, `equity`, `gross_exposure` |
| `AlertRaised` | `severity`, `code`, `message` (redacted) |

The event registry is a closed mapping type-name → model; an unknown `event_type` is a hard parse error (no silent skipping).

## 5. Acceptance criteria for CR-01 (each is an executable test)

| AC | Requirement | Test |
|---|---|---|
| AC-01.1 | Reproducible lint + format check | `ruff check` and `ruff format --check` exit 0 |
| AC-01.2 | Strict typecheck passes | `mypy --strict src/tigerquant` exits 0 |
| AC-01.3 | Full test suite passes | `pytest` exits 0 |
| AC-01.4 | End-to-end synthetic replay runs offline | integration test runs a scenario with sockets blocked |
| AC-01.5 | **Determinism**: same fixture ⇒ byte-identical journal | run twice in fresh interpreters, compare sha256 |
| AC-01.6 | No lookahead | as-of view raises on any access beyond `as_of`; indicator path tested |
| AC-01.7 | Decision schema enforcement | invalid/extra-field/wrong-action decisions rejected; `NO_TRADE` accepted |
| AC-01.8 | Evidence discipline | unknown `evidence_id` ⇒ `NO_TRADE`; stale evidence ⇒ `NO_TRADE` |
| AC-01.9 | Risk fail-closed | out-of-allowlist symbol, over-cap size, over-gross, stale mark, halted state ⇒ DENY |
| AC-01.10 | Only `ApprovedOrder` is submittable | constructing a submission from a raw intent is a type/validation error |
| AC-01.11 | Idempotent fills | duplicate fill event does not double-book inventory or realized P&L |
| AC-01.12 | Partial fill then cancel reconciles | remaining qty and realized inventory match broker-authoritative records |
| AC-01.13 | Unknown submission outcome never retries blindly | timeout scenario ⇒ `SUBMIT_UNKNOWN`, no second submit, then quarantine |
| AC-01.14 | Startup gate blocks exposure | pipeline is inert until reconciliation opens the gate |
| AC-01.15 | Mode isolation | BACKTEST/SHADOW have no submission capability; LIVE construction raises |
| AC-01.16 | Journal tamper-evidence | mutating one byte of one line fails `verify-journal` |
| AC-01.17 | Money is exact | ledger arithmetic matches an independent `Decimal` computation to the cent |
| AC-01.18 | No hidden network | `doctor` self-check + import-boundary test prove no socket use in the replay path |
| AC-01.19 | Redaction | a fake secret-shaped string in a config/log field is redacted in journal and logs |
| AC-01.20 | Failure-injection suite exists from CR-02 onward | `tests/fault/` present and wired (foundation laid in CR-01) |

## 6. Verification commands (exact, to be recorded as evidence)

```powershell
python -m pytest -q
python -m mypy src/tigerquant
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m tigerquant replay --fixture tests/fixtures/scenarios/trend_up.yaml --journal out/a.jsonl
python -m tigerquant verify-journal out/a.jsonl
python -m tigerquant doctor
```

## 7. Definition of done

- All AC-01.x tests exist and pass on a clean checkout with only `core` + `dev` dependencies installed.
- `docs/PROGRESS.md` records the exact commands, results, commit hash, known limitations and the next work item.
- No network access, no broker SDK import and no credential is required at any point in the fast lane.
- Anything simulated is labelled `SIMULATED` in code, output and docs; nothing claims paper verification.
