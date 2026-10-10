# Durable progress state

Status: CR-00, CR-01 complete. CR-02 and CR-05 substantially delivered and verified.
Current CR: CR-02 remaining item (read-only cash/position/order reconciliation), then CR-03+
Last verified commit: see `git log -1` on branch `dev`
Last successful CI run: local equivalent only (no remote CI runner configured in this environment)
Broker connectivity: **AUTHORIZED** — Tiger OAuth 2.0, read-only scopes, PAPER account verified
Paper orders: not enabled (CR-03); the credential in use could not place one
Live orders: PERMANENTLY DISABLED FOR AUTONOMOUS DEVELOPMENT
Implementation status: `PAPER_VERIFIED` for authorization and account discovery;
`SIMULATED` for everything else.

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

- **CR-02 is not finished.** Cash/position/order reconciliation is unimplemented, so the gate
  ("read and reconciled") is not met. Everything else about authorization is verified.
- **CR-03 is not started.** No order has been placed, modified or cancelled. The current token
  could not place one even if asked; a write-scoped grant would be a separate, deliberate owner
  decision with its own ADR.
- CR-04/CR-06 are not started. `nautilus_trader==1.231.0` and `litellm==1.104.2` are pinned and
  lock-resolved but not installed or exercised.
- The engine's extras and the LLM extra are not in the running environment; only `dev`, `db` and
  the minimal `broker` dependency set needed for account discovery are.
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

1. Finish CR-02: the guarded read-only client (assets, positions, open orders) plus reconciliation
   against the ledger, with the account guard applied on every call.
2. Wire the projection repository into the replay and paper pipelines so the database is actually
   fed, and add the journal-versus-database drift check (CR-09 detector).
3. CR-07 next in value: the full risk policy set (turnover, day-loss, drawdown, order rate) and the
   watchdog, since CR-03 must not ship without them.
4. CR-04 (NautilusTrader adapter) and CR-06 (model service) remain buildable offline.
