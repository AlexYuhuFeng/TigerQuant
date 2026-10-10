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
| CR-02 | **PARTIAL — NOT CLOSED** | `docs/evidence/cr02_authorization.txt` | Authorization, scope ceiling and paper-account verification are done and recorded as `PAPER_VERIFIED`. The gate's second half — reading cash/positions/orders and **reconciling** them — is **not implemented**, so the gate is not met. |
| CR-03 | **NOT STARTED** | — | No order has been placed, modified or cancelled. The current credential holds read-only scopes and could not place one. A write-scoped grant would be a separate, deliberate owner decision with its own ADR. |
| CR-04 | READY | — | Buildable offline; `nautilus_trader==1.231.0` cp314 wheel confirmed, not yet installed |
| CR-05 | **SUBSTANTIALLY DELIVERED** | `docs/evidence/cr05_database.txt`, `docs/adr/ADR-0013-database-architecture.md` | Calendar, source registry, per-category staleness, untrusted-text sandboxing, durable fact log, vintage-correct revisions, derivation lineage, and the containerised PostgreSQL layer are implemented and tested. Not marked complete: news/filing ingestion connectors do not exist yet. |
| CR-06 | READY | interfaces frozen in CR-01 (`decision/provider.py`) | Offline provider + benchmark harness buildable without a live key |
| CR-07 | READY | baseline gate in CR-01 (`risk/`) | Full policy set, watchdog, kill switch. **Should precede CR-03.** |
| CR-08 | PENDING | — | Depends on CR-05/CR-07 state to display |
| CR-09 | PENDING | fault harness laid in CR-01 | Needs CR-02/CR-03 for real paper evidence |
| CR-10 | PENDING | — | Documentation only; never activates anything |

## Current test and verification state

| Command | Result |
|---|---|
| `pytest -q` (PostgreSQL running) | **354 passed** |
| `pytest -q -m "not requires_db"` | **334 passed**, 20 deselected |
| `pytest -q -m requires_db` | **20 passed** against a real container |
| `mypy` (strict) | Success, 71 source files |
| `ruff check` / `format --check` | Clean |

## External blockers (do not paper over these)

1. **No remote CI runner.** `.github/workflows/ci.yml` is written and its commands are executed
   locally with identical results, but no GitHub run exists to link.
2. **No PyPI advisory access for `pip-audit`.** The audit command is wired into CI but could not
   run here; it is not reported as passing.
3. **Docker Hub is unreachable** from this network. `deploy/compose.yaml` pulls the same official
   Postgres image through `public.ecr.aws`; the digest is recorded in
   `docs/evidence/cr05_database.txt`.
4. **Market-data entitlements are unverified.** Quote permissions are purchased separately from
   trading access and have not been exercised.

## Resolved blockers (kept for the record)

- ~~No Tiger credentials.~~ **Resolved by the owner** on 2026-10-10: an OAuth 2.0 grant for the
  simulation paper account, read-only scopes. The live-capable account that the same grant also
  exposed is refused by the account guard, and no live access is wanted or needed.

