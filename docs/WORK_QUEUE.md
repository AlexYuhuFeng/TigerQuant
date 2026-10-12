# Work queue — execute sequentially, paper-first

| ID | Milestone | Acceptance gate |
|---|---|---|
| CR-00 | Architecture discovery, version pinning, threat model, create baseline | Docs match pinned NautilusTrader/TigerOpen/DSH APIs; explicit risk boundaries; approved component map |
| CR-01 | Build tooling, core event schemas and deterministic fake market / broker | Reproducible pytest/typecheck/lint in CI; end-to-end synthetic replay; no network/broker required |
| CR-02 | Tiger read-only PAPER integration | Account type verified via API, cash/positions/orders read and reconciled; no submit path |
| CR-03 | Tiger PAPER order integration + order state machine | Safe submit/cancel under paper-only policy; timeout/reconnect/partial-fill/unknown outcome tests |
| CR-04 | NautilusTrader adapter and shared strategy contracts | Same deterministic strategy events in backtest and paper; adapter parity tests |
| CR-05 | Market Truth Engine and point-in-time evidence store | Stale/missing/conflicting evidence blocks trades; provenance and historical vintage replay |
| CR-06 | DeepSeek via provider-neutral model interface and decision contract | Schema+evidence checks; fabricated facts rejected; NO_TRADE available; model/provider benchmarks |
| CR-07 | Hard risk gateway and watchdog | Risk tests and privilege-bypass tests; kill switch; no alternative order path |
| CR-08 | Dashboard, observability, report exports | Operator can inspect complete decision/evidence/execution lineage; isolation shown distinctly |
| CR-09 | Long-duration paper/shadow tests, faults and incident runbooks | Recovery evidence, measured false alerts, latency, P&L attribution, stability over representative sessions |
| CR-10 | Live-readiness documentation, NOT activation | Human-approved review checklist, isolated runtime plan, capital/risk sign-off requirements, remaining failure modes |

Never mark a CR done based only on docs or mock demonstrations. If paper API permissions are absent, preserve CR-02/03 as BLOCKED and proceed only with independent safe offline tasks; do not fabricate successes.

---

## Status (maintained by the agent)

Last updated: 2026-10-10.

| ID | Status | Evidence | Notes |
|---|---|---|---|
| CR-00 | **COMPLETE** | `docs/adr/` (13 ADRs), `docs/THREAT_MODEL.md`, `docs/COMPONENT_MAP.md`, `docs/DEPENDENCIES.md`, `pyproject.toml` + `uv.lock`, `docs/integrations/*`, `docs/evidence/cr00_pypi_probe.json` | Vendor APIs verified against pinned tags, not "latest" docs |
| CR-01 | **COMPLETE** | `docs/evidence/cr01_verification.txt`, `docs/evidence/replays/`, `docs/CR-01-VERTICAL-SLICE.md` | Fully offline; `SIMULATED` |
| CR-02 | **COMPLETE** | `docs/evidence/cr02_authorization.txt`, `docs/evidence/cr02_reconciliation.txt` | Gate met: account type verified via the API, cash/positions/orders read and reconciled, and no submit path exists. `PAPER_VERIFIED`. |
| CR-03 | **PARTIAL — BLOCKED on a write-scoped grant** | `docs/evidence/cr03_execution_adapter.txt` | The guarded write adapter and all four safety gates exist and are tested against a fake SDK, including timeout→UNKNOWN with no retry, partial fills, and the `order.account` hazard. **No order has ever been submitted**, because the credential is deliberately read-only. The account guard is hardened and tested (`docs/evidence/guard_write_scope.txt`): three checks before every submission, deny-by-default on live-capable account types, and a staleness bound. That is the precondition for a write-scoped grant, which remains an owner decision. Status is `SIMULATED`, not `PAPER_VERIFIED`. A write-scoped grant is an owner decision, not a coding task. |
| CR-04 | **PARTIAL** | `docs/evidence/cr04_engine_adapter.txt` | Engine installed and verified. Mode translation, the interface-shape contract, the guarded `ExecutionClient` (constructed and gated against the real wheel) and the parity harness are all done and tested. Outstanding: strategy-loop parity across backtest and paper, and parity against a real Tiger paper session (needs CR-03's grant). |
| CR-05 | **SUBSTANTIALLY DELIVERED** | `docs/evidence/cr05_database.txt`, `docs/evidence/cr05_connectors.txt`, `docs/adr/ADR-0013-database-architecture.md` | Calendar, source registry, per-category staleness, untrusted-text sandboxing, durable fact log, vintage-correct revisions, derivation lineage, the containerised PostgreSQL layer AND ingestion connectors are implemented and tested. Not marked complete because: no real feed has been contacted (entitlements are unverified, so the HTTP connector refuses by design), and a FIXTURE filing connector now exists and a filing is correctly `OBSERVED` where a wire story is `ATTRIBUTED_CLAIM` (`docs/evidence/cr05_filing_connector.txt`) -- but there is no `HttpFilingConnector`, no real filing has been fetched, and filings now get form-appropriate horizons (periodic forms 30 days, event forms 7) -- see `docs/evidence/cr05_category_policy_link.txt`, which found that NO ingested fact had been getting the policy's declared horizons at all. Revision chaining on changed content IS implemented: a re-served article becomes a supersedes revision, so effective_fact() sees the revision that actually existed at a given instant. |
| CR-06 | **PARTIAL** | `docs/evidence/cr06_model_interface.txt` | Provider-neutral path, versioned prompt contract, failure handling (every failure abstains) and the benchmark harness are done and tested. No provider has been called: that needs an API key. Status is `SIMULATED`. |
| CR-07 | **COMPLETE** | `docs/evidence/cr07_risk_controls.txt` | Session limits (turnover, daily loss, drawdown, rolling order rate), the risk-metrics accumulator, and an independent watchdog whose only power is to stop. Includes the privilege-bypass suite and the "no alternative order path" source scan. |
| CR-08 | **COMPLETE** | `docs/evidence/cr08_dashboard.txt` | Lineage from the journal (decision → evidence → risk → order states → fill → ledger), text/JSON/CSV exports, and a dashboard that is read-only and loopback-only **by construction**. Isolation is stated in the report facts, in an HTTP header on every response, and on the page itself. |
| CR-09 | **PARTIAL** | `docs/evidence/cr09_projection.txt`, `docs/evidence/cr09_drift_monitoring.txt`, `docs/evidence/cr09_metrics_runbooks.txt`, `docs/evidence/cr09_long_run.txt` | The journal→database projector covers orders, fills, positions, portfolio state, decisions and risk outcomes, verified against real PostgreSQL with idempotent replay, and drift monitoring with transition-based alerting and a measured alert rate is in place (`docs/evidence/cr09_drift_monitoring.txt`). Metrics (P&L attribution, step durations, activity counts) and eight incident runbooks are in place (`docs/evidence/cr09_metrics_runbooks.txt`). Remaining: a supervised long-running process, IN-PROCESS HANG DETECTION, backoff jitter, and evidence-fact projection. Identity is verified at startup AND on every reconnect, across all three database commands (`docs/evidence/cr09_reconnect_identity.txt`), evidence-fact projection, and MULTI-DAY runs are now possible: a full trading week (5 sessions, 1950 bars, 32.5h of market time, 8632 events) runs STABLE with no violations in 60s, having previously not finished in 10 minutes. The replay path was quadratic for three reasons, all fixed. See `docs/evidence/cr09_scaling_resolved.txt`. |
| CR-10 | **COMPLETE** | `docs/LIVE_READINESS.md`, `tests/unit/test_live_readiness.py` | The human-approved review checklist (10 preconditions, every one marked not done), the isolated-runtime plan with its two honest gaps, the remaining failure modes ranked, and the hard boundaries that survive future changes. Documentation only: it activates nothing, and a test asserts its code-checkable claims stay true. |

## Current test and verification state

| Command | Result |
|---|---|
| `pytest -q` (PostgreSQL running) | **1120 passed** |
| `pytest -q -m "not requires_db"` | **1100 passed**, 20 deselected |
| `pytest -q -m requires_db` | **20 passed** against a real container |

**The counts above are VERIFIED by `tigerquant gates`** against a record written by the last full test run. See `docs/evidence/documented_counts_verified.txt`.
| `mypy` (strict) | Success, 92 source files |
| `ruff check` / `format --check` | Clean |
| `tools/tiger_reconcile.py` | `RECONCILED=true` against the live Tiger paper account |
| `tigerquant drift` | Detects real drift (exit 1) and reports consistent after projection (exit 0) |
| `tigerquant project` | 31/31 events, idempotent on replay |
| `tigerquant report` | 6 decisions, 1 fill, lineage clean |
| `tigerquant dashboard` | Loopback-only, read-only; 405 on POST, 404 on traversal |
| `tigerquant metrics` | 6 decisions, 1 fill; durations labelled as simulated step sizes |

## External blockers (do not paper over these)

1. **CR-03 needs a write-scoped OAuth grant (`api.trade:write`), which is an owner decision.**
   Everything code-side is ready: the guard is hardened and tested, and the grant is one
   command. The remaining step is the owner approving the consent page in a browser.
   The project's grant deliberately carries four read scopes only. Every part of CR-03 that can
   be built without it has been built and tested against a fake SDK; the one remaining
   acceptance item is a real paper round trip, and it cannot be faked or worked around.
2. **No remote CI runner.** `.github/workflows/ci.yml` is written and its commands are executed
   locally with identical results, but no GitHub run exists to link.
3. **No PyPI advisory access for `pip-audit`.** The audit command is wired into CI but could not
   run here; it is not reported as passing.
4. **Docker Hub is unreachable** from this network. `deploy/compose.yaml` pulls the same official
   Postgres image through `public.ecr.aws`; the digest is recorded in
   `docs/evidence/cr05_database.txt`.
5. **Market-data entitlements are unverified.** Quote permissions are purchased separately from
   trading access and have not been exercised.

## Resolved blockers (kept for the record)

- ~~No Tiger credentials.~~ **Resolved by the owner** on 2026-10-10: an OAuth 2.0 grant for the
  simulation paper account, read-only scopes. The live-capable account that the same grant also
  exposed is refused by the account guard, and no live access is wanted or needed.

