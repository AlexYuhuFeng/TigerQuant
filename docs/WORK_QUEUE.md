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
| CR-03 | **PARTIAL — BLOCKED on a write-scoped grant** | `docs/evidence/cr03_execution_adapter.txt` | The guarded write adapter and all four safety gates exist and are tested against a fake SDK, including timeout→UNKNOWN with no retry, partial fills, and the `order.account` hazard. **No order has ever been submitted**, because the credential is deliberately read-only. Status is `SIMULATED`, not `PAPER_VERIFIED`. A write-scoped grant is an owner decision, not a coding task. |
| CR-04 | READY | — | Buildable offline; `nautilus_trader==1.231.0` cp314 wheel confirmed, not yet installed |
| CR-05 | **SUBSTANTIALLY DELIVERED** | `docs/evidence/cr05_database.txt`, `docs/adr/ADR-0013-database-architecture.md` | Calendar, source registry, per-category staleness, untrusted-text sandboxing, durable fact log, vintage-correct revisions, derivation lineage, and the containerised PostgreSQL layer are implemented and tested. Not marked complete: news/filing ingestion connectors do not exist yet. |
| CR-06 | READY | interfaces frozen in CR-01 (`decision/provider.py`) | Offline provider + benchmark harness buildable without a live key |
| CR-07 | **COMPLETE** | `docs/evidence/cr07_risk_controls.txt` | Session limits (turnover, daily loss, drawdown, rolling order rate), the risk-metrics accumulator, and an independent watchdog whose only power is to stop. Includes the privilege-bypass suite and the "no alternative order path" source scan. |
| CR-08 | PENDING | — | Depends on CR-05/CR-07 state to display |
| CR-09 | **PARTIAL** | `docs/evidence/cr09_projection.txt` | The journal→database projector and the drift check are done and verified against real PostgreSQL, including idempotent replay. Remaining: scheduling, alerting and the watchdog hook, plus projection of fills/positions/facts/decisions. The long-duration harness and runbooks are not started. |
| CR-10 | PENDING | — | Documentation only; never activates anything |

## Current test and verification state

| Command | Result |
|---|---|
| `pytest -q` (PostgreSQL running) | **475 passed** |
| `pytest -q -m "not requires_db"` | **455 passed**, 20 deselected |
| `pytest -q -m requires_db` | **20 passed** against a real container |
| `mypy` (strict) | Success, 77 source files |
| `ruff check` / `format --check` | Clean |
| `tools/tiger_reconcile.py` | `RECONCILED=true` against the live Tiger paper account |

## External blockers (do not paper over these)

1. **CR-03 needs a write-scoped OAuth grant (`api.trade:write`), which is an owner decision.**
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

