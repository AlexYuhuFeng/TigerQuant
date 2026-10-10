# Durable progress state

Status: CR-00, CR-01, CR-02 and CR-07 complete. CR-03 partial (adapter built and tested;
a real paper round trip is blocked on a write-scoped grant). CR-06 partial (provider-neutral
path and benchmark done; no live provider call). CR-09 partial (projector verified against
real PostgreSQL). CR-05 substantially delivered.
Current CR: CR-04 (NautilusTrader adapter), then CR-09 scheduling and CR-08
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

### 2026-10-10 — CR-07 complete: session risk limits, watchdog, privilege bypass

Full evidence: `docs/evidence/cr07_risk_controls.txt`.

CR-01's gateway judged one order in isolation. Session limits cannot: a daily loss limit, a
drawdown limit, a turnover budget and an order-rate cap are statements about **accumulated**
activity, so CR-07 adds the accumulator and the limits that read it.

- `risk/metrics.py` — `RiskMetrics` accumulates equity, peak equity, realised and unrealised P&L,
  notional traded and submissions. Deterministic and fed from journaled events, so a halt can be
  reproduced and explained. Peak equity is monotonic; turnover counts **notional, not orders**,
  because ten small orders and one large order carry very different risk; the trading day is
  *injected*, because it is an exchange-calendar fact and not a wall-clock one.
- Four new limits: `max_turnover_fraction` (2.0), `max_daily_loss_fraction` (0.02, one-sided — a
  gain is not a negative loss), `max_drawdown_fraction` (0.05, from the session peak), and
  `max_orders_per_hour` (30, a rolling window that complements the per-cycle cap).
- **All four fail closed when metrics are absent.** A loss limit that cannot be evaluated is not a
  limit that may be assumed satisfied. That forced every existing call site to supply metrics,
  which is the correct consequence rather than an inconvenience.
- `ops/watchdog.py` — an independent supervisor whose only power is to **stop**. It cannot trade,
  cannot approve, and cannot clear a halt (owner only). It reads metrics and conditions, never
  prose, so a model arguing for one more trade changes nothing.
- The privilege-bypass suite is the part that matters: a denied outcome cannot be upgraded to an
  approval; a forged token is refused; an approval cannot be replayed; the AI actor can neither
  halt nor clear; the watchdog cannot clear its own halt; the policy cannot be mutated at runtime;
  a check that raises denies rather than allows; and a source-tree scan asserts the broker write
  APIs appear nowhere outside the single adapter.

**Why CR-07 had to precede CR-03:** submitting orders under a policy that bounds only a single
order at a time would mean having operated without session limits, and the evidence would show it.

### 2026-10-10 — CR-09 partial: journal projection and drift detection

Full evidence: `docs/evidence/cr09_projection.txt`.

ADR-0013 claimed the journal is authoritative and the database holds *rebuildable*
projections. Until now that was a claim about intent. `db/projector.py` makes it a claim about
behaviour: it reconstructs the relational tables from the journal alone, and compares what the
journal implies against what the database says.

Verified against real PostgreSQL: 31/31 events, 5 order rows, 1 fill (SPY BUY 8 @ 602.5602),
1 position, 6 decisions, 1 risk outcome, 6 portfolio marks, and **cash 95179.48** — exactly the figure in CR-01's evidence, which is the only reason to trust
the projection. Re-running wrote nothing and changed nothing.

**Four bugs found. Two by guards that already existed, two only by running it:**

1. **A replayed projection regressed a completed order back to `CREATED`.** The fold starts
   fresh each pass, so the journal's first event re-emitted `CREATED` for an order the venue
   had already filled. Writing unconditionally would have silently rewound order state on
   every replay. The repository's terminal-state guard raised
   *"order … is terminal (FILLED) and cannot move to CREATED"*. Now a snapshot is written only
   when its event is newly appended, or when the row is genuinely missing.
2. **`EventBase.payload` is a method, not an attribute.** `getattr(event, "payload", {})`
   returns a bound method — truthy — so the failure surfaced far from its cause as
   `'function' object has no attribute 'get'`. Now a single `_payload()` helper.

A third near-miss was avoided by **deleting** code rather than patching it: the first version
reimplemented the order state machine and failed on the live run with
`illegal order transition CREATED -> SUBMITTING`, because the machine requires
`CREATED → RISK_APPROVED → SUBMITTING` and `RiskEvaluated` carries that transition. A second
implementation had no way to know. The projector now folds with the existing
`OrderProjection` and persists the result.

Two more surfaced only when the full projection ran against a clean database:

3. **The fill projection was unreachable and silently wrote zero fills.**
   `OrderPartiallyFilled` and `OrderFilled` belong to *both* the order-event set and the
   fill-event set — they advance the state machine *and* carry an execution — so written as
   `if`/`elif`, the fill branch could never run. Everything else looked right (31 events,
   correct cash, a consistent drift report); the only symptom was `0 fill(s)` for a scenario
   whose entire purpose is to fill an order.
4. **The fill payload didn't satisfy the repository's fill contract.** `OrderPartiallyFilled`
   carries the execution but not the instrument or the side — those belong to the order. The
   insert raised `KeyError: 'instrument'` and the whole projection rolled back, which is the
   transaction boundary doing its job. The payload is now enriched from the folded order, and
   a fill whose order is unknown is refused rather than given a fabricated instrument.

Drift is reported and never repaired: a divergence means we do not know which record is wrong,
and overwriting either would destroy the evidence needed to find out. The two directions are
not treated as equivalent — extra rows in the database get their own note, because the journal
is the record of authority and that direction means it was truncated or bypassed.

### 2026-10-10 — CR-03 partial: the guarded execution adapter (SIMULATED)

Full evidence: `docs/evidence/cr03_execution_adapter.txt`.

**CR-03 is partial and the missing part is not a coding task.** The adapter, all four safety
gates and the ambiguity handling are built and tested against a fake Tiger SDK. No order has
ever reached the venue, because the credential is deliberately read-only, so the status is
`SIMULATED` — not `PAPER_VERIFIED`.

Four independent gates stand between an intent and a real order, on the assumption that any one
of them could have a bug:

1. **Write-scoped credential.** `require_write_scope()` refuses to construct the client unless
   the grant includes `api.trade:write`. This is what makes the repository's default state
   "cannot submit" rather than "submits carefully".
2. **A PAPER account verified from the broker's own response** (ADR-0003 §3).
3. **A single-use risk approval**, with the adapter holding a second lock: a repeated
   `client_order_id` is refused locally.
4. **An account match at the last line before the network.** This closes a real hazard found in
   CR-02 research: `place_order` sends `order.account`, **not** `config.account`, so the SDK lets
   an order built for one account be submitted through a client configured for another.

"Never retried" is proved by counting calls after a `TimeoutError`, not by prose. There is also
no client-side idempotency claim: Tiger documents no client-order-id field with idempotency
semantics, so `external_id` is a correlation hint only.

**A correction made during this CR, worth recording.** The first version reported
`implementation_status == PAPER_VERIFIED` as soon as it held a write scope and a verified
account. That was wrong: it had never submitted anything. Holding the right configuration is a
*precondition* for paper trading, not *evidence* of it — reporting `PAPER_VERIFIED` there is
exactly the placeholder-that-appears-production-ready that AGENTS.md invariant 11 forbids. The
status is now derived from observation: it upgrades only when the venue returns an order id, and
a failed or timed-out submission does not upgrade it. Both behaviours are tested.

### 2026-10-10 — CR-06 partial: provider-neutral model path and benchmark harness

Full evidence: `docs/evidence/cr06_model_interface.txt`. Status `SIMULATED`: no provider has
been called, because that needs an API key this project does not hold.

- `decision/prompt.py` builds a **deterministic, versioned** prompt. Determinism is the point:
  without it, "the model changed its mind" and "the prompt changed underneath it" are
  indistinguishable in the journal. Untrusted content is fenced with a content hash, and fence
  markers inside the content are defanged so it cannot break out of its region.
- `decision/model.py` is the provider-neutral path. **Every failure mode returns a well-formed
  `NO_TRADE`**: outage, timeout, latency over budget, unparseable prose, empty choices, a
  non-mapping response, an action outside the schema, and a provider that answered from a
  *different model version* than the one approved. A test asserts directly that no failure
  construction ever yields `SET_TARGET`, `REDUCE` or `CLOSE`.
- `decision/benchmark.py` scores a provider over a fixed case list, through the **same
  `DecisionValidator` the pipeline uses**, so the benchmark cannot pass a decision production
  would reject. Cases that propose a trade when they should abstain are reported as *safety
  violations* rather than as a low score.

**Three real problems this work found:**

1. **The prompt told the model to emit actions the schema rejects.** The hint said
   `"BUY" | "SELL"`, but `Action` is `NO_TRADE | HOLD | SET_TARGET | REDUCE | CLOSE`. Every
   attempt to trade would have failed validation — a decision model that *looks* cautious while
   actually being unable to answer. The test now iterates the enum so it cannot drift again.
2. **A layering violation, caught by an existing boundary test.** `decision/model.py` imported
   `litellm` directly. Two boundary tests failed, both correctly. The fix was to change the
   design, not the test: the binding moved to `adapters/llm/`, and `decision/model.py` now
   *requires* an injected transport. ADR-0009 says the decision layer defines the port and the
   adapter implements it — which is what it now does.
3. **The harness initially scored a correct abstention as a failure.** A raising transport
   produces an abstention, which is correct behaviour, so the case should pass; the failing
   case is a provider that lets an exception escape `propose()`. Both are now tested
   separately, because they look identical from a distance and mean opposite things.

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

- **CR-03 is partial.** The guarded write adapter exists and is tested, but **no order has ever
  been submitted**, so its status is `SIMULATED`. The remaining acceptance item is a real paper
  round trip, which needs a write-scoped OAuth grant — an owner decision, not a coding task.
  Fills are derived at cumulative-average precision rather than per execution, because Tiger's
  `get_order` reports cumulative filled quantity; per-execution detail needs `get_transactions`
  (documented as prime accounts only) or a push subscription, which is CR-09 work.
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

1. **CR-09 continues.** Project fills, positions, facts, decisions and risk outcomes (only orders
   and portfolio state are projected today). Add a rebuild command that drops and reconstructs
   the projection tables, then schedule the drift check and alarm on it.
2. **CR-03's last acceptance item is blocked on the owner**, not on code: a real paper round trip
   needs a write-scoped OAuth grant (`api.trade:write`).
3. CR-04 (NautilusTrader adapter) is buildable offline.
4. CR-05's remaining gap: news/filing ingestion connectors.
