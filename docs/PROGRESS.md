# Durable progress state

Status: CR-00, CR-01 and CR-02 complete. CR-05 substantially delivered and verified.
Current CR: CR-07 (risk policy set and watchdog) — deliberately before CR-03
Last verified commit: see `git log -1` on branch `dev`
Last successful CI run: local equivalent only (no remote CI runner configured in this environment)
Broker connectivity: **AUTHORIZED** — Tiger OAuth 2.0, read-only scopes, PAPER account verified
Paper orders: not enabled (CR-03); the credential in use could not place one
Live orders: PERMANENTLY DISABLED FOR AUTONOMOUS DEVELOPMENT
Implementation status: `PAPER_VERIFIED` for authorization, account discovery, and read-only
cash/position/order retrieval with reconciliation; `SIMULATED` for everything else.

## How to resume

1. `git log --oneline -5` and `git status` — confirm you are on branch `dev`, not `main`.
2. Read this file, then `docs/WORK_QUEUE.md` (status table at the bottom).
3. Rebuild the environment:
   ```powershell
   $env:UV_CACHE_DIR="$env:TEMP\tq_uvcache"   # the default uv cache is outside the sandbox
   uv sync --frozen --extra dev --extra db --extra broker
   ```
4. Bring up the database and migrate it:
   ```powershell
   docker compose -f deploy/compose.yaml up -d
   .\.venv\Scripts\python.exe tools\db_migrate.py --mode backtest
   ```
5. Re-verify before changing anything:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q            # 354 with PostgreSQL up
   .\.venv\Scripts\python.exe -m pytest -q -m "not requires_db"   # 334, no infrastructure
   .\.venv\Scripts\python.exe -m mypy
   .\.venv\Scripts\python.exe -m ruff check src tests tools
   .\.venv\Scripts\python.exe -m tigerquant doctor
   .\.venv\Scripts\python.exe tools\tiger_login.py --check
   ```
6. Read `docs/adr/` before changing a boundary.

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
  Two findings materially changed the design:
  - TigerOpen has **no broker-side idempotency key** and does not auto-retry order mutations, so
    duplicate prevention rests entirely on our own reconciliation/quarantine.
  - NautilusTrader has **no PAPER environment** (`BACKTEST`/`SANDBOX`/`LIVE`), and the published
    "latest" docs describe v2, not 1.231.0.

### 2026-10-10 — CR-01 complete: offline vertical slice, CI and tests

See `docs/evidence/cr01_verification.txt` and `docs/CR-01-VERTICAL-SLICE.md`. 223 tests at the
time; deterministic replay verified byte-identical across fresh interpreters.

### 2026-10-10 — CR-02: Tiger OAuth 2.0 authorization and the paper-only guard

Full evidence: `docs/evidence/cr02_authorization.txt`.

- **Authorization works.** OAuth 2.0 loopback redirect + PKCE S256, completed by the owner in a
  browser. Dynamic client registration (RFC 7591) means no pre-registered app; there is no
  private key and no `tiger_id` anywhere in this project.
- **The credential is read-only by construction.** Tiger advertises five scopes including
  `api.trade:write`, and `OAuth2SessionManager._scopes()` requests *every* advertised scope.
  `TigerQuantOAuthSession` pins the requested set to the four read scopes and refuses a token
  whose granted scope is wider. Verified: the server echoed exactly the requested set.
  Consequence: this credential cannot place an order, whatever any module does with it.
  See ADR-0012.
- **The most important finding.** A single OAuth grant returned **both** a funded live-capable
  account (`accountType: STANDARD`) and a paper account (`accountType: PAPER`). The token is
  **not** paper-bound, and there is no separate paper authorization server. The guard — not the
  host, not the scope, not the token — is what keeps this project paper-only.
- **The guard refuses rather than guesses.** With two accounts and none configured it refused;
  asked for the live account by name it refused; only explicit selection of the unique PAPER
  account verified. Rules: broker-reported account must be present, `accountType` must be
  `PAPER` (unknown ⇒ refusal), the number must match Tiger's documented paper shape, the status
  must be usable, and multiple paper accounts require a human choice.
- Tokens and the account number live in `<workspace>/.secrets/tiger` (git-ignored) and are
  reported only in masked form. Verified that neither reaches Git.

**Still open for CR-02:** the read-only client that reads cash, positions and open orders and
reconciles them against our own ledger. Authorization and account discovery are done; the
reconciliation half is not.

### 2026-10-10 — CR-02 complete: guarded read-only client and reconciliation

Full evidence: `docs/evidence/cr02_reconciliation.txt`.

- `adapters/tiger/read.py` reads cash, positions and working orders through the verified paper
  account and compares them with our ledger. Live result: cash 1 000 000.00 USD, 0 positions,
  0 working orders, `RECONCILED=true`.
- **It cannot write.** The public surface is `verify_account`, `assets`, `positions`,
  `open_orders`, `snapshot` plus properties. A test asserts no public member matches any broker
  write API name, and the constructor re-checks rather than trusting the test. The account guard
  is re-verified on **every** snapshot, before any data is read.
- **It reports; it never corrects.** A quantity mismatch, a broker-only holding, a ledger-only
  holding or a cash difference all make `is_reconciled` false, and the ledger is left untouched.
  A test asserts the ledger digest is unchanged after a mismatch — a silent correction would
  destroy the evidence needed to find the cause (gate 15).
- **Two real-API discoveries**, both invisible from the SDK documentation:
  1. `get_assets()` and `get_open_orders()` return **plain lists**, not the `response.assets` /
     `response.result` envelopes the docstrings describe. The read path accepts both shapes.
  2. The paper environment reports **`inf`** for figures it does not compute
     (`gross_position_value`, `available_funds`, and others). `inf` is the *absence* of a value,
     not a very large one: `broker_decimal()` maps every non-finite value to `None`, the caller
     records it in `missing_fields`, and nothing is silently zeroed. Converting `inf` would have
     poisoned every downstream calculation, and `Decimal("Infinity").quantize(...)` raises anyway.

CR-02's gate — "account type verified via API, cash/positions/orders read and reconciled; no
submit path" — is met. Nothing has been submitted, modified or cancelled.

### 2026-10-10 — CR-05: Market Truth Engine and containerised PostgreSQL

Full evidence: `docs/evidence/cr05_database.txt`; decision record `docs/adr/ADR-0013`.

- **Real US equity calendar** replacing the CR-01 weekday placeholder: ten NYSE holidays with
  observation rules, Easter-based Good Friday, early closes, DST through `zoneinfo`, and
  half-day approved-order windows.
- **Source registry**: a fact may only be created for a registered source, so `source_id` is no
  longer free text. `SourceKind`, `Reliability` and `LicenseScope` are recorded per source.
- **Per-category staleness** replacing the single global horizon.
- **Untrusted text**: control-character stripping, NFC normalisation, content hashing, injection
  detection stored as quality flags, and prompt blocks that label the content as data. Flagged
  content can never admit a trade.
- **Durable append-only fact log** with a hash chain and idempotent ingestion.
- **Vintage-correct revisions**: `effective_fact()` walks the `supersedes` chain only as far as
  `as_of` allows (acceptance gate 12).
- **Derived facts** with enforced lineage; a derivation inherits the tightest input horizon.
- **Database architecture (ADR-0013)**: two tiers with fixed precedence — the JSONL journal is
  authoritative, PostgreSQL holds rebuildable projections plus the worker lease. Verified on a
  real container: **`tq_backtest` is denied CONNECT to `tq_paper` by the server**, so mode
  isolation is a credential-level fact rather than a naming convention.

## Known limitations (honest list)

- **CR-03 is not started, and deliberately so.** No order has been placed, modified or cancelled.
  The current credential holds read-only scopes and could not place one. Submitting orders before
  CR-07's turnover, day-loss, drawdown and order-rate limits exist would be the wrong order of
  work, so **CR-07 comes next**.
- CR-04 and CR-06 are not started. `nautilus_trader==1.231.0` and `litellm==1.104.2` are pinned and
  lock-resolved but not installed or exercised.
- The engine and LLM extras are not in the running environment; only `dev`, `db` and the minimal
  `broker` dependency set needed for account discovery are.
- The `broker` extra's full dependency tree was installed piecewise during debugging. Run
  `uv sync --frozen --extra broker` (or `--all-extras`) before trusting that environment; the
  lock is authoritative, not the current venv state.
- Market-data entitlements are **not** verified. Quote permissions are purchased separately.
- `pip-audit --strict` is wired into CI but its remote advisory database is unreachable from this
  sandbox, so the dependency audit has **not** been executed here.
- No remote CI runner: `main` has no recorded CI run. All evidence is from local execution of the
  exact commands CI runs.
- Docker Hub (`registry-1.docker.io`) is unreachable from this network; the compose file pulls
  the same official image via `public.ecr.aws`. Pin the digest recorded in the evidence file.

## Environment notes for a resumed session

- PowerShell/SChannel TLS is broken here (`SEC_E_NO_CREDENTIALS`); use Python for network access.
- `uv` needs `UV_CACHE_DIR` pointed somewhere writable (`$env:TEMP\tq_uvcache` worked).
- The system temp area is not writable for nested directories; `tests/conftest.py` overrides
  pytest's `tmp_path`.
- **Do not use PowerShell text round-trips (`Get-Content -Raw` + `Set-Content`) on files with
  non-ASCII characters** — it silently corrupted a UTF-8 continuation byte. Use the editor tools.
  The same applies to inline `python -c` heredocs through PowerShell: quoting breaks them; write
  a script file instead.
- Docker Desktop must be running, and the DSH file policy must not be `workspace-write`: the
  daemon is reachable only through a Windows named pipe, which workspace-write blocks. Once a
  container is up, the database is reached over plain TCP and no escalation is needed.

## Next actions

1. **CR-07 — the full risk policy set and the watchdog.** Turnover, day-loss, drawdown and
   order-rate limits, the kill switch, and the privilege-bypass tests. This is the gate that must
   be in place before CR-03 can submit anything.
2. Wire the projection repository into the replay and paper pipelines so the database is actually
   fed, and add the journal-versus-database drift check (CR-09 detector).
3. CR-04 (NautilusTrader adapter) and CR-06 (model service) remain buildable offline.
