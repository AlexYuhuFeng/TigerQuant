# Durable progress state

Status: CR-01 COMPLETE — offline vertical slice verified
Current CR: CR-02 (BLOCKED on Tiger paper credentials) → proceeding with independent safe work
Last verified commit: see `git log -1` on branch `dev`
Last successful CI run: local equivalent only (no remote CI runner configured in this environment)
Broker connectivity: NOT CONFIGURED — no Tiger credentials exist in this environment
Paper orders: not enabled
Live orders: PERMANENTLY DISABLED FOR AUTONOMOUS DEVELOPMENT
Implementation status of everything built so far: **SIMULATED** (no external system has been contacted)

## How to resume

1. `git log --oneline -5` and `git status` — confirm you are on branch `dev`, not `main`.
2. Read this file, then `docs/WORK_QUEUE.md` (status table at the bottom).
3. Rebuild the environment:
   ```powershell
   $env:UV_CACHE_DIR="$env:TEMP\tq_uvcache"   # the default uv cache is outside the sandbox
   uv sync --frozen --extra dev
   ```
4. Re-verify before changing anything:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   .\.venv\Scripts\python.exe -m mypy
   .\.venv\Scripts\python.exe -m ruff check src tests tools
   .\.venv\Scripts\python.exe -m tigerquant doctor
   ```
5. Read `docs/CR-01-VERTICAL-SLICE.md` before touching the slice, and `docs/adr/` before changing a boundary.

## Development log

### 2026-10-10 — CR-00 complete: architecture discovery, pinning, threat model

- Repository had no Git history; baseline specification snapshot committed to `main`
  (`chore: baseline specification snapshot`), then all work moved to branch `dev`. `main` is
  treated as protected.
- Wrote eleven ADRs (`docs/adr/`), the threat model (`docs/THREAT_MODEL.md`), the component map
  (`docs/COMPONENT_MAP.md`) and the CR-01 specification (`docs/CR-01-VERTICAL-SLICE.md`).
- Pinned versions from live PyPI metadata (`tools/pypi_probe.py`,
  `docs/evidence/cr00_pypi_probe.json`): `nautilus_trader==1.231.0`, `tigeropen==3.8.0`,
  `litellm==1.104.2`, `pydantic==2.14.0`, `jsonschema==4.26.0`, `PyYAML==6.0.3`,
  `tzdata==2026.5`. Python range `>=3.12,<3.15`; development interpreter CPython 3.14.7.
- Researched the two vendor SDKs against their **pinned tags** and wrote citation-backed notes:
  `docs/integrations/tigeropen_api_notes.md`, `docs/integrations/nautilus_api_notes.md`.
  Two findings materially changed the design and are recorded in the ADRs:
  - TigerOpen has **no broker-side idempotency key** and does not auto-retry order mutations, so
    duplicate prevention rests entirely on our own reconciliation/quarantine.
  - NautilusTrader has **no PAPER environment** (`BACKTEST`/`SANDBOX`/`LIVE`), and the published
    "latest" docs describe v2, not 1.231.0. A real Tiger paper account is `Environment.LIVE`
    plus our account guard; `Environment.SANDBOX` is a different, locally-simulated thing.

### 2026-10-10 — CR-01 complete: offline vertical slice, CI and tests

**Delivered** (`src/tigerquant/`, 56 modules):

| Area | Modules |
|---|---|
| Core | `core/{enums,errors,clock,ids,money,canonical,redaction}` |
| Events | `events/{base,market,decision,risk,execution,portfolio,registry,journal,emitter}` |
| Market | `market/{fixtures,feed,asof,session}` |
| Evidence | `evidence/{facts,store,snapshot,ingest}` |
| Decision | `decision/{contract,parsing,provider,validation}` |
| Risk | `risk/{policy,state,approvals,gateway}` |
| Execution | `execution/{broker,fake_broker,intent,service}` |
| Strategy | `strategy/{base,reference}` |
| Portfolio | `portfolio/{ledger,sizing}` |
| Wiring | `pipeline/replay.py`, `cli.py`, `config/settings.py` |

**Exact commands and results** (full log: `docs/evidence/cr01_verification.txt`):

| Command | Result |
|---|---|
| `python -m ruff check src tests tools` | All checks passed |
| `python -m ruff format --check src tests tools` | 71 files already formatted |
| `python -m mypy` (strict, pydantic plugin) | Success: no issues found in 56 source files |
| `python -m pytest -q` | **223 passed** |
| `python -m tigerquant doctor` | 4/4 safety self-checks PASS |
| `python -m tigerquant replay --fixture tests/fixtures/scenarios/trend_up.yaml --journal …` | 1 order submitted, 1 filled, 8 SPY, cash 95 179.48, equity 100 015.48 |
| `python -m tigerquant verify-journal …` | OK: 31 records, hash chain valid |

**Determinism evidence** — two independent processes, same fixture, raw journal file SHA-256
identical (`5ac9de744ae629f15e5c79a4289f7f60a4db39760431324cfd5899c60c6439d3`). Also asserted in
the suite for all four scenarios, and under two different `PYTHONHASHSEED` values
(`tests/test_determinism_crossprocess.py`).

**Acceptance criteria** (`docs/CR-01-VERTICAL-SLICE.md` §5) — all 20 implemented and passing:

| AC | Where |
|---|---|
| AC-01.1–3 lint/type/test | see table above |
| AC-01.4 offline end-to-end | `tests/integration/test_replay_slice.py::TestOfflineGuarantee` |
| AC-01.5 byte-identical replay | `tests/test_determinism_crossprocess.py` |
| AC-01.6 no lookahead | `tests/unit/test_market_asof.py`, `tests/fault` |
| AC-01.7 schema enforcement | `tests/unit/test_decision_validation.py::TestSchema` |
| AC-01.8 evidence discipline | `TestValidator` (fabricated/absent/stale/contradictory) |
| AC-01.9 risk fail-closed | `tests/unit/test_risk_gateway.py` |
| AC-01.10 only `ApprovedOrder` submittable | `test_risk_gateway.py::TestApprovals`, `tests/integration/test_execution_service.py` |
| AC-01.11 idempotent fills | `test_execution_state_machine.py`, `test_execution_service.py` |
| AC-01.12 partial fill then cancel | `test_execution_service.py::test_partial_fill_then_cancel_reconciles_exactly` |
| AC-01.13 no blind retry | `tests/fault`, `unknown_outcome` scenario |
| AC-01.14 startup gate | `test_execution_service.py::TestStartupGate` |
| AC-01.15 mode isolation | `tests/unit/test_config_mode.py` |
| AC-01.16 journal tamper-evidence | `tests/unit/test_journal.py` |
| AC-01.17 exact decimal arithmetic | `tests/unit/test_core_primitives.py`, `TestTrendUpScenario` |
| AC-01.18 no hidden network | socket-blocked replay test + subprocess import assertion |
| AC-01.19 redaction | `tests/unit/test_core_primitives.py::TestRedaction` |
| AC-01.20 failure-injection suite | `tests/fault/test_failure_injection.py` (19 cases) |

**Bugs found and fixed during CR-01** (each now covered by a test):

1. The order projection was never fed by the journal, so every order looked like a first attempt
   and every fill was classified as partial. Fixed by making projections observers of the emitter
   and by rebuilding them from the journal on restart.
2. A venue acknowledgement carried the *post-fill* remaining quantity, so a 4-of-8 partial fill
   was journaled as a complete fill. Fixed by stating the position before the fills.
3. `_build_context` failed on a missing mark before the allowlist check could run, reporting
   `CHECK_ERROR` instead of the specific denial. Fixed; missing marks now reach their own check.
4. Risk evaluation only caught `TigerQuantError`; any other exception escaped instead of failing
   closed. Fixed to catch everything and deny.
5. Contradiction detection ignored conflicts *between cited facts*, so citing both sides of a
   disagreement produced a COMPLETE snapshot. Fixed.
6. `Mode` was compared case-sensitively against `config/example.yaml`, which uses lower case.

## Known limitations (honest list)

- Everything is `SIMULATED`. No Tiger, NautilusTrader or LLM integration has been executed. The
  optional extras are pinned and lock-resolved but the adapters do not exist yet (CR-02/03/04/06).
- The evidence store is in-memory and the exchange calendar is a weekday/time window, not a real
  holiday/half-day calendar (CR-05).
- The risk policy covers allowlist, long-only, order/symbol/gross caps, buying power, mark
  freshness, market window, halt and unreconciled-intent blocking. Turnover, day-loss, drawdown,
  order-rate and margin checks are CR-07.
- A rejected intent is terminal but not blocking, so the rule may retry on a later bar (each retry
  is a separate journaled intent). A per-session order-rate cap belongs in CR-07.
- Journal rotation/archival is not implemented (CR-09); journals grow monotonically.
- `pip-audit --strict` is wired into CI but its remote advisory database is unreachable from this
  sandbox, so the dependency audit has **not** been executed here. This is an environment
  limitation, not a passing check.
- The `main` branch has no CI run recorded: this environment has no remote runner. All evidence is
  from local execution of the exact commands CI runs.

## Environment notes for a resumed session

- PowerShell/SChannel TLS is broken in this sandbox (`SEC_E_NO_CREDENTIALS`); use Python for any
  network access (`uv`, `python -m pip`, `tools/pypi_probe.py`).
- `uv` needs `UV_CACHE_DIR` pointed somewhere writable (`$env:TEMP\tq_uvcache` worked).
- The system temp area is not writable for nested directories; `tests/conftest.py` overrides
  pytest's `tmp_path` fixture to a repository-local directory for that reason.
- Avoid PowerShell text round-trips (`Get-Content -Raw` + `Set-Content`) on files containing
  non-ASCII characters: it silently replaced a UTF-8 continuation byte here. Use the editor tools.

## Next actions

1. CR-02/CR-03 are BLOCKED: they require a Tiger paper account, which does not exist in this
   environment. Build the adapter and its fake-SDK contract tests offline, labelled `SIMULATED`.
2. CR-04 (NautilusTrader adapter + parity) is buildable offline and is next in sequence.
3. CR-05, CR-06, CR-07 are buildable offline behind their existing interfaces.
4. CR-10 documentation can be written at any time and must never enable anything.
