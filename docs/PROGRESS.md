# Durable progress state

Status: CR-00, CR-01, CR-02 and CR-07 complete. CR-03 partial (adapter built and tested;
a real paper round trip is blocked on a write-scoped grant). CR-06 partial (provider-neutral
path and benchmark done; no live provider call). CR-09 partial (projector verified against
real PostgreSQL). CR-05 substantially delivered.
Current CR: gates verify cases AND counts; CR-03 and CR-06 await the owner's decisions
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

### 2026-10-10 — CR-04: the engine adapter, built against the source

Full evidence: `docs/evidence/cr04_engine_adapter.txt`.

**An earlier note in this file was wrong.** I had recorded the NautilusTrader install as failed
after three attempts. It had actually **succeeded** — the non-zero exit came from PowerShell
treating the process's *stderr progress output* as a `NativeCommandError`. `nautilus_trader
1.231.0` imports fine. On this platform, exit code alone is not evidence about a tool that
writes progress to stderr; the version check is. That correction is recorded because it nearly
cost a whole CR.

**ADR-0010 was right, and it mattered.** The ADR says the published guides describe the v2
Rust-core line and cannot be trusted for 1.231.0. Reading the `.pyx` that ships *inside the
wheel* found two differences that would each have made the adapter silently non-functional:

| documented (v2) | actual 1.231.0 |
|---|---|
| `__init__(loop, client_id, venue, …)` | `__init__(client_id, venue, oms_type, account_type, base_currency, msgbus, cache, clock)` |
| `async def _submit_order(command)` | `cpdef void submit_order(SubmitOrder command)` |

An adapter written from the guides would have imported cleanly and **never intercepted a single
order** — the private coroutine names don't exist, so nothing would have been called. A third
fact came from the same source: `_set_account_id` raises unless `client_id ==
account_id.get_issuer()`, which my first `AccountId("TIGER-PAPER")` violated.

Everything verified is now pinned in `adapters/nautilus/compat.py`, so a wheel bump that moves a
class, renames a method or changes the environment enum fails loudly.

The guarded `ExecutionClient` **constructs and connects against the real engine**, with three
tested gates: the submission gate (refuses and *records* when `submission_target` is `NONE`), the
approval check (no path submits without an `ApprovedOrder`), and translation (refuses an unknown
side, order type, missing price or bad quantity). Cancellation is deliberately **not** gated —
a risk control that can block a cancel is dangerous.

**A control this CR had to fix rather than work around.** The privilege-bypass scan for "no
alternative order path" searched for literal strings like `submit_order(` and produced two false
positives: the Nautilus adapter legitimately *defines* `submit_order` to satisfy the engine's
override points, and a docstring names the APIs it forbids. A control that forces code to avoid
certain words is a control nobody keeps, so the scan now uses the **AST** and looks for call
nodes. Verified afterwards that it still catches a real violation.

### 2026-10-10 — CR-09 part 2: drift monitoring with measured alerting

Full evidence: `docs/evidence/cr09_drift_monitoring.txt`.

Part 1 built the drift *check*: compare the journal against the database and report. Reporting
once, on demand, is not monitoring. The design constraint is in the gate's own wording —
**"measured false alerts"** — so the monitor alerts on **transitions** rather than on state:

* first drift → `WARNING`;
* still drifting → silence, unless the reminder cadence is due;
* drifted for `escalate_after` consecutive checks → `CRITICAL` **once**, because a drift that
  does not clear by itself is a different situation from one that comes and goes;
* drift clears → an `INFO` recovery alert, so the operator need not re-check to find out.

Twenty checks of one unresolved drift produce exactly **two** alerts — announcement and
escalation — giving `alerts_per_drifted_check = 0.10`. A second test lowers the cadence to 1s
and asserts the rate rises *above* 0.5, so the shouting failure mode is visible in the
measurement too. Both directions are covered because both are failures.

**Live, and not staged:** the monitor found genuine drift — the integration suite truncates the
same `tq_backtest` database it runs against — reported it, and exited 1. Projecting the journal
made the next check report consistent and exit 0.

**What it deliberately does not do.** It never *repairs* drift (a divergence means we do not
know which record is wrong, so overwriting either destroys the evidence). It never *halts* — it
reports `blocks_new_exposure` as a fact and the watchdog decides. And it emits nothing when
given no emitter, which is a real trap: emitting the alert *into* the journal would append an
event the database does not yet have, so the **next** check would report drift caused by the
monitor itself. That is why the CLI passes no emitter.

### 2026-10-10 — CR-10 complete: live-readiness review (documentation, not activation)

Deliverable: `docs/LIVE_READINESS.md`.

This CR is documentation only, and the document is written so that it could not be mistaken for
a runbook. It does not describe how to turn live trading on; it describes what a **human** would
have to work through before live trading could be *considered*, and what is honestly not ready.

- **Ten hard preconditions**, each requiring a **named human** and written evidence, each marked
  **not done**. Two deserve emphasis because an implementation-focused review would miss both:
  *market-data entitlements* are a commercial fact, not a code fact, and are currently
  **unverified**; and the *risk policy values* (2% daily loss, 5% drawdown, 2× turnover) are
  engineering judgement, not a calibration to anyone's capital or tolerance.
- **Why paper success is not sufficient**, stated concretely: no queue position, no market
  impact, no partial-fill reality, no real rejections, no latency, and indicative rather than
  entitled data. Every one is a reason a paper result *overstates* live behaviour.
- **Isolated-runtime plan** with the built and unbuilt parts marked separately. The honest gaps:
  there is no worker container, and no per-mode alert routing.
- **Remaining failure modes, ranked worst first.** The top entry is not an engineering failure at
  all — a strategy that is wrong in a way that is profitable in backtest. Overfitting and
  survivorship bias are statistical failures that no amount of test coverage detects.
- **Hard boundaries** that survive every future change, including that an agent never relaxes a
  risk limit and never presents a paper result as live-verified.

**The claims are code-checked.** `tests/unit/test_live_readiness.py` asserts that `Mode.LIVE`
still refuses translation, that no non-paper mode reports submission capability without explicit
human authorization, that the project status is not `LIVE_VERIFIED`, that the safety checks are
all passing, and that the document contains no step-by-step activation procedure and no
precondition marked done. A readiness document that quietly goes stale is worse than none,
because it is exactly what a human would rely on.

### 2026-10-10 — CR-08 complete: lineage, dashboard and report exports

Full evidence: `docs/evidence/cr08_dashboard.txt`.

The gate's two halves drove the design. "Complete decision/evidence/execution lineage" defines
the data structure; "isolation shown distinctly" defines what must be impossible to misread.

- `ops/lineage_report.py` assembles one record per decision carrying **every hop**: the snapshot
  it was shown, the evidence it cited versus what the snapshot contained, the risk verdict with
  the checks that ran, the order intent, the venue order, the state sequence, the fills, and the
  ledger afterwards. It reads the **journal**, not live objects, so it describes what actually
  happened.
- **Fabricated citations would be visible, not merely rejected.** `evidence_not_shown` is cited
  minus shown. Validation already rejects such a decision, so it is always empty in a healthy
  run — but if it ever is not, the operator *sees* a fabrication in the lineage rather than
  inferring it from a rejection code. The CLI exits 1 when a report is not clean, so it works as
  a gate.
- **Isolation is stated in three places at once**, because the failure it guards against is
  reading one mode's numbers as another's: in the report facts with an explicit note, in an
  `X-TigerQuant-Mode` **header on every response including errors**, and in a banner on the page.
- **The dashboard is read-only and loopback-only by construction**, not by policy. Only GET and
  HEAD are answered; the constructor refuses any host outside loopback (tested against `0.0.0.0`,
  a LAN address, `::` and a hostname); a fixed five-entry route table means a request path is
  only ever a dictionary lookup; and there is no JavaScript, with every interpolated value
  escaped — including ones we generate ourselves.

**A real bug the tests found:** responding to a refused method without draining the request body
left unread bytes in the socket, so the connection reset and the client saw
`ConnectionAbortedError` instead of the 405 the server had sent. The test that POSTs a *body* is
what caught it.

### 2026-10-10 — CR-09 part 3: measured metrics and incident runbooks

Full evidence: `docs/evidence/cr09_metrics_runbooks.txt`.

**The most important thing the metrics module does is refuse to mislead.** CR-09's gate says
"latency", but the offline replay drives an injected `SimulatedClock`, so a duration from those
timestamps is a **fixture step size** and says nothing about a real venue. So `LatencyReport`
carries a `clock_source`, `is_observed` is a property derived from it, the label reads *"step
duration (simulated clock) — NOT a latency measurement"*, and the default is the conservative one
so a real measurement requires passing `SystemClock` deliberately.

**The P&L attribution caught a real property of the event stream.** Its first run reported *does
not reconcile*, and it was right. `PositionChanged` is emitted when a position *changes*,
carrying the mark that applied then, while `LedgerMarked` keeps being emitted as the mark moves —
so the last position snapshot is from an earlier instant than the final valuation, and the
difference is unattributable from these events alone. Rather than hide it, the report names the
`residual` and explains it, and `metrics` exits 1 when it exceeds a cent.

A quieter bug came out of the same work: `sum_of_parts` summed the per-instrument breakdown, so a
report carrying aggregates but no instrument rows returned `-commission` — confidently wrong. It
now uses the aggregates, with a separate `breakdown_agrees` check, because a report can reconcile
with the ledger while its own breakdown contradicts it.

**Eight incident runbooks**, each in three parts: what you will see, what to do, and what NOT to
do. Two matter most. RB-03 says explicitly *do not* regenerate the hash chain from the database,
because the database was built *from* the journal and rebuilding it would launder a corruption
into a consistent-looking history. RB-08 is not an incident at all — it is the boundary, and its
instruction is to decline and point at `LIVE_READINESS.md`.

The exit-code table is a contract, so it is checked: `test_runbooks.py` verifies every subcommand
the runbooks name actually exists (read from the argparse parser, not a hardcoded list), that the
drift runbook's quoted threshold and cadence match the configured policy, and that the cancel
exemption RB-04 claims is true of `RiskState`.

### 2026-10-10 — CR-05 complete: evidence ingestion connectors

Full evidence: `docs/evidence/cr05_connectors.txt`. This closes the last gap CR-05 had named —
nothing actually *fetched* anything.

Three separations carry the safety of the module:

1. **A connector produces documents, not facts.** Assigning the fact id, hashing content and
   stamping availability happen in `IngestionPipeline`, in one place. A connector therefore
   cannot mint a fact id, backdate availability, or mark its own content clean.
2. **`available_at` is stamped at ingestion, never taken from the source.** The source's
   timestamp says when *they claim* they published; what matters for lookahead is when *we*
   could first have acted on it.
3. **A connector that cannot be verified refuses to run.** Entitlements are unverified here, so
   `HttpNewsConnector` raises and says what would be needed — and with an entitlement it *still*
   refuses, because a fetcher that has never run against a real endpoint is untested code
   wearing a working label.

Live deterministic run: 2 documents admitted, 1 **injection-flagged**, facts stored as
`ATTRIBUTED_CLAIM` — not `OBSERVED`, because a news article is somebody's assertion and this
system did not observe it. A second pass admitted 0 and found 2 duplicates.

**A lookahead guard in the `Fact` model that this tripped, correctly.** A source whose clock ran
a minute fast produced a validation error, because `Fact` refuses `event_time` later than
`available_at` — which is right, since a fact whose event precedes our knowledge of it *is*
lookahead. The fix belongs in ingestion, not the model: `event_time` is now `min(published, now)`
while `published_at` keeps the source's claim.

**A claim I withdrew rather than made true.** The first version stripped text before hashing and
claimed a reformatted refetch would be idempotent. It isn't: leading whitespace on a body becomes
*interior* whitespace once joined with the title. Folding interior whitespace would have made the
claim true at the cost of conflating documents that genuinely differ — the wrong trade for an
evidence store. The real behaviour is now asserted, which exposes a real gap: **revision chaining
is not implemented**, so a feed that reformats its articles would accumulate near-duplicates.

### 2026-10-10 — Account-guard hardening: the precondition for write scope

Full evidence: `docs/evidence/guard_write_scope.txt`.

One OAuth grant returns **both** a funded live-capable account and the paper account. There is no
separate paper issuer and the token is not paper-bound, so the **guard is the only thing keeping
this project paper-only** — which is why it got this treatment before any grant.

**Three checks before every submission**, all at the write call site, in increasing specificity:
the scope is **re-read** (a token can be re-scoped under a running process); the broker is **asked
again** (how a grant that gains a live account mid-session is caught); and `assert_verified_is_paper`
refuses a live-capable type. `is_live_capable` is **deny-by-default** — only an explicit `PAPER`
is not live-capable, because an unrecognised type must fail as a refusal rather than an acceptance.
A staleness bound (300s) stops a long-running process trading on a check from this morning.

19 tests, several asserting the stronger property: when re-verification fails, **`place_order` is
never called**. Refusing after the order is sent would be worthless.

**A real hole in the determinism control, found by this work.** Writing the guard's staleness
check added `dt.datetime.now(...)` — and `doctor` still said "determinism: clean". The detector
matched `ast.Attribute` on an `ast.Name`, so it caught `datetime.datetime.now()` but **not**
`dt.datetime.now()`: `import datetime as dt` defeated it entirely.

Closing that hole surfaced **seven real offenders** that had been invisible all along. Split by
consequence: the **adapter's** timestamps reach the *journal*, so they now come from an injected
clock — a wall-clock read there would break byte-identical replay. The guard's staleness check
takes an injected clock too. `settings.py` and `model.py` are on an explicit **named** allow-list
(filenames, not a pattern) because they timestamp an operator artifact or measure a real interval
and neither contributes to a journaled event.

Ruff then caught the fix itself (B023, late binding): my resolver captured the loop variable
`aliases`, so it would have resolved every file against the *last* file's aliases and the check
would have passed by alphabetical accident.

### 2026-10-10 — The write-scope ceiling becomes an explicit opt-in

Full evidence: `docs/evidence/scope_ceiling.txt`. This makes CR-03's authorization *possible*; it
does not grant it and places no order.

**The project's own guard blocked the grant.** The CR-02 session refused any token carrying a
write scope — correctly, as defence against a server that ignores the requested set — which meant
the moment the operator approved `api.trade:write`, every TigerQuant session would refuse to load
the token. The control worked exactly as designed and made CR-03 impossible.

**The rule changed, and the new one is stronger.** Old: *"never hold a write scope."* New:
**"never hold a scope we did not request."** The check is a set difference against the requested
set, not a blocklist, so with the default (four read scopes) the effect is identical to before —
and with the opt-in, anything *other* than the write scope that appears is still refused. A
blocklist refuses the scopes someone thought to list; a set difference refuses every scope that
was not asked for, including ones nobody has invented yet.

**The opt-in is a constructor parameter defaulting to False**, so every other module stays
read-only without having to say so. Only `tools/tiger_login.py --enable-order-submission` sets it,
and that flag prints what it implies — including that the token is *not* paper-bound — **before**
the authorization URL, so the warning is on screen while the consent page is read.

One test was wrong on the first run and is worth recording: it asserted that a read-only session
refuses an *exact-match* grant of the read scopes, which is not what "refuse anything not
requested" means. The assertion was right to fail; corrected rather than papered over.

### 2026-10-10 — CR-05 revision chaining: the recorded gap, closed

Full evidence: `docs/evidence/cr05_connectors.txt` section 7. This was the gap I found *while
writing* the connectors evidence and recorded rather than smoothed over — a feed that re-served
an article with a corrected figure left **two facts that both looked current**, and a snapshot
could cite either. The Fact schema already anticipated it (`supersedes`, `vintages()`,
`effective_fact()` all existed) and the field was left `None`.

The rule now: a revision is the same **document** (same source, same external id) with different
**content**. Identical content stays a duplicate; a different document starts its own chain.

```
$ ingest "Chipmaker raises guidance to 5.10."
fixture-news: fetched 1, admitted 1, duplicates 0
$ ingest "Chipmaker raises guidance to 5.25 (corrected)."
fixture-news: fetched 1, admitted 1, duplicates 0, revisions 1

fact:e9b754e740a6fb22d  supersedes=None
fact:b8a720fab13f5c8f7  supersedes=fact:e9b754e740a6fb22d

chain from newest: 3 revisions
oldest first bodies: ['to 5.10', 'to 5.25 (corrected)', 'to 5.30 (corrected again)']
```

Three properties worth naming. **Nothing is overwritten** — the chain is a link, not an update,
so the store stays append-only. **The link is made before the store sees the fact**, so there is
no window in which two revisions both look current. And **indexing is lazy and reads the store**,
so a pipeline started against an existing fact log chains onto what is already there — the case
that matters operationally, because a restart is exactly when a feed re-serves something. That
one is tested by building a *second* pipeline over the same store with no memory of the first.

The payoff is not cosmetic: `vintages()` and `effective_fact()` now work on ingested content, so
a replay at an earlier instant sees the revision that actually existed then.

**One test asserted the old behaviour and was rewritten, not deleted.** It documented the gap by
asserting `all(fact.supersedes is None)`; it now asserts the chain, with the old reasoning kept
in the docstring. Five more cover the byte-identical case, a third revision, a different
document, the reported count, and the restart.

### 2026-10-10 — CR-09 part 4: long-duration stability harness

Full evidence: `docs/evidence/cr09_long_run.txt`. One full US equity session ran clean:

```
long-run [STABLE]; 1 simulated session(s), 390 bars; covering 6.50h of MARKET time in 7.35s of
wall time (53.1 bars/s); 2158 event(s), 390 decision(s), 1 submitted, 1 filled, 197 risk denial(s)
  [x] journal hash chain verifies after the whole run
  [x] journal and database agree after projecting the whole run
  [x] the run is reproducible: two passes produce the same journal digest
  [x] every fill traces back to a decision
  [x] the risk limits were exercised (a limit that never fires is not evidence)
violations: (none)
```

**Two clocks, never merged.** `simulated_span_hours` is market time; `wall_seconds` is machine
time. There is deliberately no combined "duration" field, because that is the same misreading this
project already had to correct for latency.

**Two things I had to fix in my own work.** The report *claimed* two invariants it never checked —
the list was written before the checks, which is the worst version of the mistake, because the
report then reads as evidence for something nobody verified. Both are now actually run and can
fail. And the harness first sat in `ops/`, which sits *below* the pipeline it orchestrates; this
project's own boundary test caught the upward import. That is the second time that test has caught
a file placed by convenience rather than by layer.

**A real performance finding, surfaced by the harness doing its job.** The first five-session run
did not finish in ten minutes. Measuring: **100 bars → 0.54s, 200 → 1.76s (3.3×), 400 → 8.20s
(4.7×)**. Doubling the bars multiplies the time by ~4 where linear work would double it. The
replay path is **O(n²) in bars**, and that is why the multi-day run did not complete. Recorded,
not worked around — likely the snapshot builder or the as-of view rescanning accumulated facts per
bar, but that is a hypothesis and has *not* been investigated.

**Consequence for the gate, said plainly:** stability is demonstrated for one full session and is
reproducible; a multi-day run needs the quadratic behaviour fixed first. Not extrapolated from a
short run.

### 2026-10-10 — CR-09 part 5: the O(n^2) replay scaling, two of three causes fixed

Full evidence: `docs/evidence/cr09_scaling_investigation.txt`. **Partial, and recorded as
partial**: two causes are fixed and measured, a third is localised but NOT isolated.

**I profiled before touching anything**, because the previous writeup named "the snapshot builder
or the as-of fact view" as a hypothesis, and a hypothesis is not a cause.

**Cause 1 — `vintages()` scanned every fact, per walk step.** It found successors by scanning
`self._facts.values()`, and was called **20,888 times in a 200-bar run** — about a hundred times
per bar. A revision chain must be linear, so at most one successor exists: `self._successor:
dict[str, str]`, maintained in `add()`.

**Cause 2 — `_find_contradictions()` compared every cited fact against every fact.** The inner
loop was over the whole store, though contradiction is scoped to
`(instrument, category, event_time)`. Now `self._by_key`, also maintained in `add()`. Both indexes
live at the single place a fact enters the store, so there is no rebuild path to forget.

| bars | before | after |
|---|---|---|
| 100 | 0.54s | 0.53s |
| 200 | 1.76s | 1.46s |
| 400 | 8.20s | **5.31s** |
| 800 | — | 16.38s |

Real (400 bars is 35% faster) but **still not linear**: 2.7x, 3.6x, 3.1x per doubling where
linear would be ~2x.

**Cause 3 — NOT ISOLATED.** Re-profiling puts the remaining cost in **canonicalisation**, not the
fact store: `to_canonical` 4.378s over 2.85M calls, driven by `snapshot.build()` once per bar.
Calls per bar are roughly *constant* (~7,100 canonical nodes), yet the *rate* falls ~4x between
the 100-bar and 800-bar runs. A constant number of digest calls whose per-call cost grows is not
explained by anything found.

**Deliberately not fixed.** The snapshot digest is load-bearing provenance (invariant 6) and sits
inside the journal digest CR-01's baseline pins. Changing what is canonicalised would move that
baseline, and a change made at the end of a session to make a benchmark faster is exactly the
change that should not be made quickly.

**A mistake worth recording:** the first attempt broke 49 tests and errored 116 more.
`EvidenceStore` declares `__slots__`, and the two new attributes were not in it. The failure was
immediate and total, which is the good outcome.

15 new tests pin both indexes, so a future change cannot silently reintroduce the scans.

### 2026-10-10 — CR-09 part 6: the O(n^2) replay scaling RESOLVED, multi-day runs possible

Full evidence: `docs/evidence/cr09_scaling_resolved.txt`. Supersedes the PARTIAL status of the
previous round.

**The run that could not finish in ten minutes now takes 60 seconds.**

```
long-run [STABLE]; 5 simulated session(s), 1950 bars; covering 32.50h of MARKET time in 28.14s
of wall time (69.3 bars/s); 8632 event(s), 1950 decision(s), 1 submitted, 1 filled,
275 risk denial(s); journal sha256:49c917391bf00560...
violations: (none)
```

**Cause 3, ISOLATED.** Last round I localised the cost to canonicalisation and said plainly it was
not isolated. Instrumenting `digest_of` to record what it was handed settled it in one run:
5,360 calls at 100 bars and 81,798 at 400 -- about 0.5 x bars^2 -- with `max cited facts` equal to
the bar count. `resolve()` emits `effective.digest()` for every CITED fact, and the momentum
strategy cites every bar it has seen, so snapshot N digests N facts.

**Checked for a correctness bug first, and it is not one.** `resolve()` iterates only the cited
ids, so a decision's provenance holds the facts it used, not the whole store. A growing citation
window is legitimate. The problem was purely that the same immutable bytes were digested
repeatedly.

**The fix: memoise `Fact.digest()`.** A Fact is frozen, so the digest is a pure function of state
and a cached value is the SAME BYTES. That is exactly why this was safe where changing the digest
*material* would not have been -- CR-01's baseline journal digest pins the snapshot digests.
Verified unmoved: 776 tests pass, including the 27 that pin the baseline.

| bars | original | after causes 1-2 | after cause 3 |
|---|---|---|---|
| 100 | 0.54s | 0.53s | 0.52s |
| 200 | 1.76s | 1.46s | **1.04s** (2.0x, linear) |
| 400 | 8.20s | 5.31s | **2.82s** |
| 800 | -- | 16.38s | **7.54s** |

The remaining 2.7x per doubling is **not** claimed to be linear, and no further profiling was done
because the gate no longer requires it.

**A question this raised and did NOT answer:** the strategy citing every bar is what made this
quadratic. It is legitimate, but whether a decision's provenance *should* grow without bound is a
design question about evidence scoping, not a performance one. Recorded as a limitation.

### 2026-10-10 — CR-09 part 7: alert routing

Full evidence: `docs/evidence/cr09_alert_routing.txt`. New module `ops/alerts.py`, 35 tests.

**What was missing:** the drift monitor decided *whether* something was wrong and suppressed its own
repeats, but nothing left the process. `DriftCheckResult` could report `alerted=True` and no human
would ever see it. **An alert that is only counted is not an alert.**

**Two properties carry the weight, and both are about failure modes rather than success.**

**NO NETWORK BY DEFAULT.** `WebhookSink` is the only network path and it **refuses to exist**
without an explicit http(s) endpoint — an empty string, a bare host, `ftp://` and `file://` are all
refused. `urllib.request` is imported *inside* `deliver()`, and a test scans the source to assert
it stays there, because the import location is the guarantee. Same posture as `HttpNewsConnector`
refusing to fetch without an entitlement reference.

**A SUPPRESSED ALERT STAYS COUNTABLE.** "Nothing happened" and "everything was suppressed" are
different states, and a router that renders them identically hides an outage behind a quiet log.
Every suppression is recorded with its reason. A test asserts the distinction directly: a quiet run
and a fully-suppressed run both deliver zero, and only one has suppressions to show for it.

**Suppression is per-sink and transition-based**, so a condition seen by two sources is one page
per sink rather than two. `clear(key)` exists because without it a problem returning after an hour
would stay suppressed for ever — the failure mode of every naive deduplicator. Severity floors are
per sink, because "what is worth waking me up" differs per channel.

**A failing sink is visible and does not cascade**: it is recorded with its exception, one bad sink
does not stop the others, and a failed delivery is *not* marked delivered — so the next occurrence
is not suppressed by a delivery that never happened.

**Not done, and stated as such:** the module is not yet wired to a CLI command or to the drift
monitor's loop. It is a tested library, not a running alerting path, and the evidence file says so
in those words.

### 2026-10-10 - CR-09 part 8: alert routing wired into the drift monitor

Full evidence: `docs/evidence/cr09_alert_wiring.txt`. Part 7 built the router and nothing called
it; an alerting path that exists but is unwired is indistinguishable from none, which invariant 11
forbids.

**Routing now happens inside `check()`**, not in the caller. If callers had to convert results and
call `route()` themselves, the first one that forgot would stop alerting silently. `emitter` and
`router` are separate fields because they fail differently: the emitter writes to the journal
(record of authority), the router reaches an operator. A monitor with an emitter and no router
reports drift into a journal nobody is watching -- exactly the state this project was in until now.

**A broken alerting path never breaks reconciliation.** `_route()` catches everything; if it did
not, a drift would also silence its own warning. Tested with a raising sink: `check()` still
returns, history is still recorded, and a failed delivery is not marked delivered -- so the next
occurrence is not suppressed by a delivery that never happened.

**AN UNRESOLVED DEFECT, MARKED xfail RATHER THAN HIDDEN.** The condition-cleared path does not work
as intended: after the pair is made to agree (by *projecting* the events -- an empty repository is
itself a divergence), `check()` reports consistent and `_route` calls `router.clear(...)`, yet the
suppression entry is still present afterwards. So a condition that resolves and returns may be
delivered only once ever. It is `xfail(strict=True)` rather than deleted or skipped, so the gap is
visible in every run, fixing it turns the test green, and strict mode then forces the marker to be
removed deliberately. **Had I deleted it, the suite would read as fully green on a behaviour nobody
has demonstrated.**

**A test bug worth recording:** the first version passed `empty_journal()` to `check()` while
seeding the divergence in the *events*, so three tests skipped with "the seeded pair already
agrees". A diagnostic showed the drift detector working correctly and the *test* at fault -- the
skip message pointed at the implementation.

### 2026-10-10 - CR-09 part 9: the cleared-condition defect, fixed

Full evidence: `docs/evidence/cr09_alert_recurrence.txt`. **No xfails remain in the suite.**

Last round I marked a test `xfail(strict=True)` rather than deleting it, saying the clear either
did not run or did not take. This round found out which, and it was the first: **an early return.**

```
41:  self._history.append(result)
42:  return result                      <-- consistent branch: NO _route call
...
95:  self._history.append(result)
96:  self._route(result)               <-- drift branch: routes
97:  return result
```

My original patch replaced the LAST `append(result); return result` in the method, which is the
drift branch. The consistent branch returns earlier and never reached it, so `clear()` was never
called and a condition that resolved and came back stayed suppressed **for ever**.

`_route(result)` is now called on the recovery path too. **The test passes unchanged** -- it
described the intended behaviour correctly all along and the implementation did not satisfy it.
That is the argument for marking a known-bad test xfail rather than deleting it.

**A mistake I made while fixing it, recorded because it is a repeat.** The edit's match included
the line `self._state.consecutive_drifted += 1` and the replacement did not, so the fix DELETED
the drift counter. I caught it by re-reading the file immediately after the edit and restored the
line. `test_the_drift_counter_still_increments` now asserts it explicitly.

**7 new tests, and the important one is behavioural rather than internal.** The round-19 test
asserted on `router._last_delivered`; this one asserts what an operator experiences:

    drift appears      -> 1 alert
    drift resolves     -> (no alert, but recorded as recovered)
    drift RETURNS      -> 2 alerts

plus: repeats within one outage are still suppressed (the fix did not turn suppression off), a
second outage pages once rather than once per cycle, the recovery is still recorded in history,
and two structural tests that count `self._route(result)` occurrences so a future refactor
reintroducing an early return fails here rather than in the field.

### 2026-10-10 - CR-09 part 10: the `tigerquant alerts` command group

Full evidence: `docs/evidence/cr09_alert_cli.txt`. The last CR-09 item reachable offline: sinks are
now configurable and testable from the command line rather than only from Python.

```
tigerquant alerts show     what is configured, and what each sink will accept
tigerquant alerts verify   route a synthetic CRITICAL through EVERY sink; exits non-zero on failure
tigerquant alerts drift    reconcile a journal against an empty database and route it
```

**`verify` is the one that earns its place**, and the reason is the reason the alerting path
exists at all: an alerting path is only ever exercised when something is actually wrong -- the
worst possible moment to discover it does not work -- and it fails SILENTLY, because the whole
point is that nobody is watching it. `verify` exercises it on demand. Its exit code (0 clean, 1
any sink failed) makes it a deployment gate rather than a report.

Both failure paths were checked by hand as well as by test: a bare host and an `ftp://` URL are
REFUSED with exit 2, not skipped. Skipping a misconfigured sink silently is how an operator ends up
believing they have alerting when they do not.

**The determinism control caught this module immediately.** Adding `alert_cli.py` broke `doctor` on
the first run -- `offenders: alert_cli.py:datetime.datetime.now()` -- and two tests failed with it.
The wall-clock read is legitimate here ("now" is correct for an on-demand probe, and nothing is
replayed), so it went on the operational allow-list *with that reason recorded*. Worth noting as
the payoff from closing the detector's aliasing hole two rounds ago: before that fix, this exact
call would have been invisible.

`verify` defaults its probe to `out/alerts_verify.jsonl`, not the real alert log, so a probe never
pollutes what an operator reads during an incident.

### 2026-10-10 - CR-09 part 11: the supervised observation loop

Full evidence: `docs/evidence/cr09_supervisor.txt`. New module `ops/supervisor.py`, 35 tests.

**Three ways an unsupervised loop fails badly, and what this does about each.**

**A hung process looks exactly like a healthy one.** A dead process is easy -- the OS notices. A
process that is ALIVE BUT STUCK looks identical to a working one. So the supervisor writes a
heartbeat **before and after** every cycle, carrying the cycle index and phase. Writing it twice is
the design: a single write at the end cannot tell "working" from "stuck in cycle 41", because a
stuck process has simply not written yet. With a start-of-cycle write, a watchdog sees the **index
stop advancing** -- the actual failure it needs to detect.

**Looping for ever on a broken system hides the breakage.** After N consecutive failures it
**halts**, and records the halt as a CRITICAL alert so a stopped supervisor is not silent.
Retrying indefinitely produces a process that is up, is logging, and is reconciling nothing -- the
state most easily mistaken for health.

**It never repairs anything**, inheriting the drift monitor's stance: a divergence means we do not
know which record is wrong, so overwriting either destroys the evidence needed to find out.

**It does not trade, structurally.** The cycle is an INJECTED CALLABLE, not an imported
dependency, so the class cannot reach a venue even if someone wanted it to. Three tests assert the
module contains no `submit_order`/`place_order`/`OrderIntent`, imports nothing from
`tigerquant.adapters`, and says so in its docstring. A long-running process is exactly where an
accidental trading loop would be most damaging and least visible.

**A cycle that FINDS drift is a success.** `drift_detected` and `ok` are separate fields --
counting a check that ran correctly as a failure would halt the supervisor for doing its job,
which is backwards. And a transient failure does not halt: the counter is CONSECUTIVE and resets on
any success.

**Two defects the tests caught in my own code.** The halt reason was DISCARDED -- `run()` built a
detailed message naming the last error, then passed only the code, so whoever found the process
stopped learned *that* it halted but not *what* broke it. It now carries both. And `as_record()`
emitted a float, which `digest_of` refused **correctly** -- this project does not canonicalise
floats because they are not reproducible across platforms. It is a string now.

    max_consecutive_failures: 2 consecutive cycle(s) failed; last error:
    RuntimeError: database stopped answering. Halting rather than retrying: a loop that keeps
    running against a broken system is the state most easily mistaken for health.

### 2026-10-10 - CR-09 part 12: the `tigerquant supervise` command

Full evidence: `docs/evidence/cr09_supervise_cli.txt`. Closes the gap part 11 recorded: the loop
had no way to be started.

    tigerquant supervise <journal.jsonl> [--interval 30] [--iterations N] [--max-failures 3]
                                      [--heartbeat out/supervisor.json] [--no-alerts]

**It cannot trade, and there is no flag that would let it.** No broker option, no account option,
no approval registry anywhere in the module -- the cycle only ever READS a journal and compares it
with a database. Three tests assert the source contains no order vocabulary, imports nothing from
`tigerquant.adapters`, and says so in `--help`. **Enforced by what is absent**, which is stronger
than a check that could later be removed.

**It says what it is.** Against an in-memory repository it is a wiring check, and the command
prints that in its own banner rather than leaving the operator to infer it. PostgreSQL is not wired
to it -- the single biggest remaining gap, and a wiring job rather than a design one.

**Drift is reported as a success.** The output says so in words: "a SUCCESSFUL check reporting bad
news, not a failing supervisor". And a self-halt **exits 2** rather than 0, so whatever supervises
the supervisor can tell "stopped because it broke" from "stopped because someone asked".

Live run: 3 cycles, 3 ok, 0 failed, digest emitted, heartbeat on disk carrying the cycle index.

**Three rounds running, the determinism check has caught new code within seconds** -- `alert_cli.py`,
`supervisor.py`, now `supervisor_cli.py`. Each went on the operational allow-list with its reason
recorded rather than the rule being loosened.

### 2026-10-12 - CR-09 part 13: the supervisor reconciles against PostgreSQL

Full evidence: `docs/evidence/cr09_supervise_postgres.txt`. **This exhausts CR-09's offline work.**

    $ python -m tigerquant supervise out/pg/j.jsonl --dsn "postgresql://tq_backtest:.../tq_backtest"
    database          PostgreSQL via PostgresRepository
    supervisor: 1 cycle(s), 1 ok, 0 failed; worst severity WARNING; stopped normally

`--dsn` or `TIGERQUANT_DB_DSN` -- the SAME variable `tigerquant drift` already uses, so one setting
covers both commands rather than two names for one thing.

**A connection failure is a REFUSAL, never a fallback.** It exits 2 and reconciles nothing. A
supervisor that appears to reconcile while actually comparing the journal against an empty dict is
**worse** than one that will not start, because it reports health it has not checked -- precisely
what invariant 11 forbids.

**A defect found by hand:** pointing the command at an unreachable database produced a raw driver
exception and **exit 1**, not the exit 2 the command intends. The `except` caught only this
project's own error types and the driver raises its own hierarchy. **Exit 1 from a traceback is
indistinguishable from a crash for any other reason**, which defeats the point of a distinct code
for "the database is down". Fixed by catching broadly on purpose and reporting narrowly;
`_select_repository` extracted so the logic is testable on its own.

**And a test-performance defect I introduced:** the first version of those tests took **391
seconds** for ten assertions, because a FILTERED port times out rather than refusing. Two attempts
at a "fast failing" port hit the same wall; the answer was not a better dead port but a
`connect_timeout=2`. **391s -> 6.5s.** Worth recording because the suite is what would have
degraded quietly -- 6.5 minutes added to a 20-second fast lane is how a suite gets skipped, and a
skipped suite is worse than a slow one.

It still cannot trade, re-asserted AFTER the wiring: a database connection is the most plausible
route by which an order path could creep into a reading loop, so the absence is asserted again.

### 2026-10-12 - CR-09 part 14: a backoff tier between "fine" and "halted"

Full evidence: `docs/evidence/cr09_supervisor_backoff.txt`. New `BackoffPolicy`, 20 tests.

**THE SAFETY PROPERTY IS THE CLASSIFICATION, NOT THE RETRYING**, and getting it backwards would
make the supervisor worse rather than better. Retrying is right for a database that blinked and
WRONG for a bug: hammering a broken code path produces exactly the state this module exists to
avoid -- a process that is up, is logging, and is achieving nothing -- while a brief outage should
page nobody.

So the classifier **defaults to NEVER**: an unclassified failure is PERMANENT and halts, because
retrying a defect for ever is the worse of the two mistakes since it looks like health. Only a
small explicit set of database-shaped failures is transient, matched by exception NAME because the
`db` extra is optional and this module must import without it -- weaker than isinstance, stated
rather than hidden, and mitigated by the permanent default.

**Two counters, deliberately.** `max_consecutive_failures` counts defects; the transient streak
counts outages. Sharing one would let a flapping database spend the budget that exists to catch a
real defect. The halt reasons are distinct for the same reason: `max_consecutive_failures` is a
defect, `transient_exhausted` is an outage that outlasted its budget, and an operator acts
differently on each.

**`max_attempts` is bounded on purpose.** "Retry for ever" is the one policy that guarantees the
process outlives the incident it is supposed to report, and nobody notices it stopped working.

Observed: two `DbDown` failures retried with backoff then recovered (4 cycles, not halted); a
`ValueError` halted at cycle 1 **despite 8 attempts of budget available**; an unrecovering outage
halted with `transient_exhausted` after exactly 8.

**The most important remaining gap, recorded as a limitation:** a backoff retry re-reads the
journal but REUSES the held repository connection, so against a database that actually restarted
the retry would fail again for a reason the backoff cannot fix.

### 2026-10-12 - CR-09 part 15: reconnecting the repository, not just the cycle

Full evidence: `docs/evidence/cr09_reconnect.txt`. New `db/reconnect.py`, 20 tests.

**The gap was the worst shape of defect.** The backoff tier retried the CYCLE while the monitor
held the original connection, so against a database that had ACTUALLY RESTARTED -- the case backoff
exists for -- every retry failed identically and the loop halted having never once tried a fresh
connection. **The retry logic was correct, its tests passed, and it could not have worked in the
one situation it was built for.** Nothing short of a real restart would have revealed it.

**`ReconnectingRepository` lives in `db/`, not in the supervisor**, because the supervisor
supervises a CALLABLE and knows nothing about databases. Making the repository reconnect underneath
keeps that boundary -- the monitor, the cycle and the supervisor all needed **no changes**. One new
module, one wiring line.

**It reconnects on the NEXT call and propagates the failure UNCHANGED**, so the supervisor's
classifier still sees the real exception rather than a proxy's opinion of it. Reconnecting inside
the failing call would hide the failure from the backoff accounting.

**A non-connection failure does not drop the connection** -- one bad query must not become a
reconnect storm -- and the `is_dead` predicate is INJECTED, so the proxy does not hardcode what a
dead connection looks like.

**The proxy is lazy, and that caught my own tests.** Six of the first twenty failed with
`IndexError` because they killed `connections[0]` before making a call, and no connection existed
yet. **The tests were wrong and the design was right** -- worth recording because the failure
looked like a bug in the proxy. `_select_repository` primes it at startup anyway, so a bad DSN is
still reported before the loop begins.

`reconnects` is reported on the command line, because "it recovered" and "it never actually
dropped" are different claims.

### 2026-10-12 - CR-09 part 16: the database says which mode it serves

Full evidence: `docs/evidence/cr09_database_identity.txt`. New `db/identity.py` + migration, 23 tests.

**The defect was a FALSE NEGATIVE, which is why it outranked everything else left.** Every mode has
its own database and a DSN is just a string, so passing the PAPER DSN while running
`--mode BACKTEST` connected to the paper database, compared an EMPTY paper database against a
backtest journal, found the expected divergence, and **reported a clean run against the wrong
database**. Nothing noticed -- **a database with no rows looks exactly like a database with no
rows** -- and the failure mode is the dangerous direction: reporting health it has not checked.

Now:

```
$ tigerquant supervise ... --mode PAPER          # against the BACKTEST database
refusing to use this database: this database declares itself 'BACKTEST' but 'PAPER' was expected
```

**Why this does not contradict ADR-0013 §2.** The mode is still NOT on any row and no query filters
by it; the database boundary remains the isolation mechanism. `database_identity` is a SINGLE ROW
DESCRIBING THE DATABASE ITSELF, so a process can assert it connected to the right one. A guard
against misconfiguration, not a substitute for the boundary. The `CHECK (id)` primary key makes
"at most one row" structural.

**Deny by default in three places:** undeclared is REFUSED rather than assumed to match (and the
escape-hatch verdict says plainly that the database *cannot be checked*); a **mismatch is refused
even with that flag**, since the escape hatch is for an unverifiable database and never for a
demonstrably wrong one; and **declaring is the operator's explicit act**, because a tool that
stamped whatever mode it was told to would make the guard agree with any mistake. Re-declaring a
different mode is refused outright -- a database whose mode silently changed is worse than one that
refused to start.

**A test caught unreachable code in my own implementation.** `verify_identity` has an "unreadable"
branch for a read that failed; the first `read_identity` caught EVERY exception and returned None,
so that branch **could never run** and a connection reset was reported as "undeclared". The same
class of mistake as the `_INVARIANTS` list naming checks nobody performed. Fixed by distinguishing
SQLSTATE 42P01 from a real failure.

### 2026-10-12 - CR-09 part 17: the identity guard covers every database command

Full evidence: `docs/evidence/cr09_guard_coverage.txt`. 14 new tests.

**A guard that covers one of three entry points is not a guard; it is a guard plus two ways around
it.** All three now refuse the wrong database with the same message and exit 2, and the check plus
its two flags live in a shared helper so the wording cannot drift between commands.

**The insertion bug is the interesting part.** The script that added the guard used
`text.replace(anchor, guarded, 1)` -- replacing the FIRST occurrence. **The guarded text CONTAINS
the anchor.** So the second pass found the anchor again inside the already-guarded `_cmd_drift`,
nested a SECOND guard there, and left `_cmd_project` completely unguarded.

The behavioural check found the missing one (`project` did not refuse). **The double guard in
`drift` was silent** -- harmless today, a latent oddity tomorrow. Three things now argue against a
recurrence: the fix script **asserts the property** (counts the guard per handler and refuses to
continue unless each has exactly one); the anchor must be a **whole line**, because matching it as
a substring broke the file twice more and left it unparseable, caught immediately by ruff; and the
test suite now **counts**, so the silent half is covered too.

**And the tests found a real defect: `--declare-mode` produced a traceback.** `declare_mode` RAISES
when a database already declares a different mode, so passing `--declare-mode` against the wrong
database produced an uncaught `ConfigurationError` instead of a clean refusal. **The refusal was
correct; its delivery was not** -- a traceback is indistinguishable from a crash, and an operator
who passes the wrong DSN *and* a helpful extra flag deserves the same message as one who does not.
**A refusal is a decision, not an exception to leak.**

### 2026-10-12 - CR-09 part 18: identity is re-verified on every connection

Full evidence: `docs/evidence/cr09_reconnect_identity.txt`. 12 new tests.

**A reconnect is exactly the moment the identity guarantee can silently stop holding** -- a
failover to a replica, a DNS change, a load balancer moving the connection elsewhere. Any of them
can land the process on a DIFFERENT database and the run would carry on reporting against it.

**The same false-negative shape as the original defect, one layer down.** Not "we connected to the
wrong database" but **"we connected to the right one, and then quietly moved."**

`ReconnectingRepository` gains an optional `verify_on_connect`, called with EVERY connection. A
failure to verify **closes the rejected connection** rather than keeping a working repository
pointed at the wrong database, **propagates unchanged** so the caller sees a refused reconnect, and
is treated as **PERMANENT** by the supervisor's classifier -- right, because landing on the wrong
database is not something to retry. It does **not inflate `reconnects`**, since that number must
mean *successful* reconnections or it lies about how healthy the loop was.

Proof, with a scripted sequence where the reconnect lands elsewhere:

```
startup verify: ['BACKTEST'] -> rows
RESULT: refused -> refusing: database is PAPER, expected BACKTEST
verifications performed: ['BACKTEST', 'PAPER']       <- it DID re-verify
connected after refusal: False                        <- nothing was kept
```

**One rule, not two.** The first connection goes through the same path as a reconnection, so they
cannot disagree. And a refusal does not poison the proxy: once the database comes back correct the
next call works, because a guard that permanently disabled the loop after one bad reconnect would
turn a transient failover into an outage. Verification runs once per CONNECTION, not per call.

**Honest boundary:** the scenario is proven with a SCRIPTED connection sequence, not a real
failover. Nothing here stops a container, changes DNS, or moves a load balancer. The logic is
tested; the integration is not.

### 2026-10-12 - The acceptance gates become machine-checkable

Full evidence: `docs/evidence/gate_verification.txt`. New `ops/gates.py` and the `tigerquant gates`
command. **Until now every round ended with me ASSERTING "the gates are met" in prose.** That claim
was never checkable.

Each of the sixteen critical test cases maps to the actual pytest node ids that prove it:

```
$ python -m tigerquant gates
critical test cases: 14/16 proven, 2 partial, 0 unproven
```

Exit 1 when any case has nothing behind it. **PARTIAL does NOT fail**: a case with a named gap is an
honest state, and a gate that fails on every honest gap gets disabled -- which protects nothing.

**THE MODULE CAUGHT ITS OWN AUTHOR, TWICE, ON THE FIRST RUN.** Ten mapped test files did not exist:
I wrote plausible filenames from memory and **missed an entire `tests/fault/` failure-injection
directory** where most of the cases are actually proven -- the suite AGENTS.md requires from CR-02
onward, which I had forgotten existed. Then, having fixed the filenames, every `tests/fault/` id was
still rejected because they omitted class names. The tool was right both times.

**The person writing the coverage table is the least reliable source of truth about it**, and this
caught me within seconds of being written.

**And it caught a defect in its own logic.** The first version reported "14/16 proven" while printing
`MISSING TEST(S)` beneath ten of them -- the status came from the table's claim alone and ignored
whether the mapped tests existed, so the report contradicted its own evidence on one screen. The
docstring promised a gap would "render as UNPROVEN, never as passing" and the code did not do it.
**A coverage report that overstates coverage is worse than no report**, because it removes the
reason to look. Fixed with a single `effective_status` so the summary, the counts and the rendered
lines cannot disagree.

**The two PARTIAL cases, named:** case 4 (order timeout) and case 11 (provider resilience). Both are
exercised against fakes and neither has been seen against the real thing, because **no order has
ever been submitted and no provider call has ever been made**. Both are blocked on the same two
external decisions as CR-03 and CR-06 -- neither is a code gap.


**A stale-count hazard found while updating the status table.** docs/WORK_QUEUE.md still read
"**731 passed**", several rounds out of date. The cause is that these updates are read-modify-write
scripts over a shared document, and one of them wrote back a copy read before another had updated
it -- the classic lost update. Nothing detected it: a stale test count is not a test failure, and
the document is prose.

**What would catch it:** a check that the counts in WORK_QUEUE.md match a real run, which is now
the natural job of 	igerquant gates -- it already collects the suite and knows how many tests
exist. Until that exists the counts are hand-maintained and can rot silently, which is exactly what
happened here.

### 2026-10-12 - Documented test counts are verified against a real run

Full evidence: `docs/evidence/documented_counts_verified.txt`. 25 tests. **Closes the stale-count
gap found two rounds ago, after one failed attempt and a revert.**

**The defect:** `WORK_QUEUE.md` read "731 passed" for several rounds -- a lost update in a
read-modify-write script over a shared document. **Nothing detected it, because a stale test count
is not a test failure.**

**The first attempt failed** because spawning `pytest --collect-only` HANGS here -- confirmed with
`capture_output=True` AND with the child's stdout sent to a file. **Subprocess execution itself does
not complete in this environment**, not merely piped stdio. That attempt was reverted.

**What works is better than the original design.** A `pytest_collection_finish` hook records what
collection actually saw, and `gates` compares the documents against it:

```
$ python -m tigerquant gates
documented count [OK] pytest -q: claims 1069, actual 1069
documented count [OK] pytest -q -m "not requires_db": claims 1049, actual 1049
documented count [OK] pytest -q -m requires_db: claims 20, actual 20
```

The documents are compared against **the run that actually happened**, not a hypothetical second
collection by a second interpreter.

**Three defects found while building it.** *Two absences compared equal*: with both the document and
the record missing, `claimed == actual` was `-1 == -1` and reported **OK** -- the one outcome a gate
must never produce. *A subset run clobbered the record*, so full-suite claims were compared against
a fragment and reported stale when they were correct. And *the `render()` patch silently did not
land*, so the counts were computed, correct, and **invisible** -- the JSON and exit code were right
while the human-readable report omitted the section. That one was found by asserting on rendered
text rather than on underlying data.

**Proven against the exact historical failure:** setting the document back to "731 passed" gives
`ok: False, STALE DOCUMENTED COUNTS: 1`. And a missing record fails too (`actual -1`), rather than
defaulting to success.

### 2026-10-12 - CR-05: a fact's epistemic kind is DERIVED from its source

Full evidence: `docs/evidence/cr05_fact_kind_mapping.txt`. 13 tests.

**The gap.** `_to_fact` hardcoded `kind=FactKind.ATTRIBUTED_CLAIM` for every ingested document, with
a comment explaining that a news article is somebody's assertion. **That reasoning is correct for
news and wrong for everything else** -- a regulatory filing ingested through the same pipeline would
be labelled a mere claim.

Worse, the distinction had been **anticipated and then not built**: `SourceKind.OFFICIAL_FILING`
already existed in the enum. Nothing produced it and nothing honoured it. A half-built thing that
reads as complete -- the class of defect this project's own rules are aimed at.

**The axis is PRIMARY versus SECONDARY, not true versus false.** A filing and a wire story can report
the same number and are still different evidence. Collapsing them lets a secondary report carry a
primary document's weight, which is what invariant 4 exists to prevent.

**The deeper problem was that the mapping was INLINED as a single literal**, so there was nowhere to
express a second case. It now lives in `_SOURCE_KIND_TO_FACT_KIND`, and `fact_kind_for()` **REFUSES**
an unmapped kind rather than defaulting -- a default would be wrong in one of two directions, and
**nothing downstream could detect either, because the label IS the evidence.**

**A test that caught a better question than a bug.** Three of my first thirteen tests failed, passing
a bare string and getting an `AttributeError` from the error message. The failure exposed that
**every current `SourceKind` IS mapped**, so the refusal is unreachable through the enum today. That
is not dead code -- it is defence against the NEXT kind added -- but it has to be tested as such. The
tests now remove a mapping via `monkeypatch` and assert the refusal, plus a separate test that no
current kind is unmapped. Testing it with a string would have exercised a different path and called
it coverage.

**The count check earned its place this round**: adding these tests made the documented counts stale,
and `gates` said so before I noticed.

**Not built, and named as such:** there is still no connector producing `OFFICIAL_FILING` documents.
This round fixed the mapping a filing connector needs. That remains the CR-05 gap.

### 2026-10-12 - CR-05: the filing connector, and the distinction it exists to express

Full evidence: `docs/evidence/cr05_filing_connector.txt`. 19 tests. Closes CR-05's recording gap:
"there is no connector producing OFFICIAL_FILING documents."

**The payoff, through the SAME pipeline:**

```
10-Q filing   -> FactKind.OBSERVED            the document itself
wire story    -> FactKind.ATTRIBUTED_CLAIM    somebody's account of it
```

Asserted three ways: a filing alone, a wire story alone, and both together with the kinds compared
side by side. They can report the same number -- **collapsing them lets a secondary report carry a
primary document's weight**, which is what invariant 4 exists to prevent. Before last round's
source-derived mapping, both would have been ATTRIBUTED_CLAIM and nothing would have noticed.

**What makes a document a FILING is not the connector's class name** -- that would be a label, not a
fact. The source is registered as `OFFICIAL_FILING` with `AUTHORITATIVE` reliability, and the
pipeline reads THAT to decide the kind. A connector cannot promote its own output. The accession
number is the external id, because it is what a citation should use. And the form type is validated
against a closed set: an unrecognised form is refused rather than stored, because it would be marked
OBSERVED and then **weighted against nothing**.

**It is deliberately NOT a subclass of `FixtureNewsConnector`.** The two are structurally identical,
and a shared base class would invite a caller to treat them interchangeably -- precisely the confusion
the epistemic-kind distinction exists to prevent. **The opposite of the usual DRY instinct, and the
right call: the difference between these connectors is not implementation, it is MEANING.**

**Not built, and named:** no `HttpFilingConnector`, so EDGAR is not contacted. The fixture proves the
PIPELINE, not a feed. And **no staleness horizon is set for filings yet** -- a 10-Q currently inherits
the conservative default, the same horizon as a news article. That is safe and wrong in spirit;
`PERIODIC_FILING_FORMS` records the distinction and no policy consumes it yet.

### 2026-10-12 - CR-05: the pipeline produced categories the policy did not name

Full evidence: `docs/evidence/cr05_category_policy_link.txt`. 19 tests. Found while closing last
round's stated limitation, and **the real defect was worse and silent**.

**The defect.** `DEFAULT_STALENESS` declares seven categories with chosen horizons -- `news_claim` at
one hour, `filing_claim` at thirty days. The pipeline derived its category from the SOURCE KIND NAME
(`OFFICIAL_FILING`, `NEWS_WIRE`), **which appears nowhere in that policy**. So every ingested fact
fell through to the 60-second default, and the two horizons the policy was written for were
consulted by nothing.

**Why it mattered, and why nobody would notice.** Stale evidence BLOCKS TRADES (invariant 4), so
every ingested fact became unusable one minute after ingestion -- and an operator who configured a
thirty-day filing horizon would see it have no effect. **A fact that reads as stale looks exactly
like a fact that IS stale.** No error, no warning; a legitimate filing simply gets refused, with a
reason that is truthful and misleading at once.

**The fix is the same shape as the fact-kind mapping two rounds ago**, because they are the same
defect: a source's KIND determines both the epistemic status of its facts and their staleness
category, and both were being decided by a name-shaped guess. `MARKET_DATA` is deliberately left
unmapped -- a market-data document is not necessarily a price bar, and guessing would be the same
mistake in a new place.

**The `periodic` flag finally does something.** A 10-K describes a period and stays useful for weeks;
an 8-K announces a moment and does not. The flag has been recorded since `filing_document` was
written and **nothing consumed it until now**: periodic forms get 30 days, event forms 7.

**A test encoded the defect.** `test_a_clean_document_becomes_an_attributed_claim` asserted
`fact.category == "NEWS_WIRE"` -- the value the code produced, not the property that mattered. It
passed for as long as the bug existed. **A test that asserts what the code DOES will follow the code
anywhere, including into a defect.** It now asserts the category AND that the policy resolves it to
3,600 seconds.

**And my own failing assertion found a discrepancy.** I asserted the default is the most conservative
value; it is not -- the docstring says it "must be the most conservative value", while the validator
only enforces that it is no looser than the LOOSEST category. **The docstring claims a guarantee the
code does not provide.** Bounded in effect, now asserted in both directions rather than silently
corrected in the test, and left unfixed because either fix has a real blast radius.

### 2026-10-12 - The same defect for the third time, so the COMPARISON is now tested

Full evidence: `docs/evidence/category_consistency.txt`. 19 tests. **No new feature.** What this
round produced is a test that closes a whole CLASS of defect rather than the instance visible at the
time.

**The pattern, three times.**

- **Round 34.** The pipeline emitted categories derived from source-kind names (`OFFICIAL_FILING`,
  `NEWS_WIRE`) which appeared nowhere in `DEFAULT_STALENESS.per_category`. Every ingested fact fell
  through to the 60-second default and the policy's thirty-day filing horizon was consulted by
  nothing.
- **Round 35 / this round.** `KNOWN_CATEGORIES` was missing `filing_event` (added to the policy a
  round earlier) and `derived_feature` (never added). It is also **exported and never consumed** -- a
  constant that looks like validation and validates nothing.
- **And four policy categories have no producer at all.**

Each is the same thing: **a list on one side of an interface disagreeing with a list on the other,
with nothing comparing them.**

**The corrections are three lines. The value is the comparison.** The policy's keys,
`KNOWN_CATEGORIES`, the source-kind mapping and the set of categories a producer actually emits are
now asserted equal, with failures that say which direction is wrong. The reserved set is an EXACT
equality: **adding a horizon with nothing to use it now fails the suite and says so.**

`PRODUCED_BY_EVIDENCE_INGESTION` is written out **explicitly rather than derived** -- "which
producers exist" is a fact about the code that a test should state out loud, and a derived set would
move with the code, which is exactly how last round's test came to encode a defect.

**A limitation asserted rather than described.** `KNOWN_CATEGORIES` is still not enforced, so a
typo is stored and silently takes the conservative default -- the same shape as the defect it was
just corrected for. The tests state that as a FACT: one asserts the constant is absent from `Fact`,
another **builds a fact with a deliberate typo** and shows it is accepted. Wiring it in is the right
change and has real blast radius (existing tests use `"earnings"`), so it is recorded rather than
made late in a round.

### 2026-10-12 - The dependency audit RAN, and found something

Full evidence: `docs/evidence/dependency_audit.txt`. The gates require "package dependency security
checks"; every previous round recorded this as **NOT EXECUTED** rather than passing, because the
advisory service appeared unreachable.

**It was worth retesting.** `pip-audit 2.10.1` is installed and every advisory endpoint is now
reachable -- including `github.com`, which timed out in earlier rounds. **A limitation recorded once
and never rechecked is a limitation that may no longer be true**, and this one was carrying a gate
item.

```
fsspec 2026.2.0  CVE-2026-104851  fix: 2026.6.0
```

**It cannot be fixed here, and that is established rather than assumed.** `nautilus_trader==1.231.0`
pins `fsspec==2026.2.0` EXACTLY -- `uv lock` refuses the floor as unsatisfiable -- and **1.231.0 is
the latest release**, so there is no upgrade path either. The floor was added, refused, and
**reverted**; `pyproject.toml` records why rather than carrying a constraint that cannot lock.

**The pinning discipline CR-04 exists to enforce is exactly what makes this unfixable.** That is not
a criticism: an engine whose API was verified against a specific release is worth more than a
transitive patch bump, and the alternative is re-verifying the adapter against an unreleased version.

**Exposure, as far as it goes.** `fsspec.open` appears in nautilus only in `backtest/__main__.py`,
`live/__main__.py` and the Parquet persistence layer. **TigerQuant invokes none of them** -- it uses
the engine only through `adapters/nautilus/execution_client.py`. That is **a reduction in likelihood,
not a proof of safety**: whether the CVE needs a crafted URL, a malicious archive, or something
reachable through a config value is **unknown, and has NOT been assessed.**

**What is NOT claimed:** the audit does not pass; the vulnerability is not fixed; exposure is not
proven zero; and the CVE's nature is not established. What this round did is locate the risk and
establish that it is unfixable here -- which is a different thing from clearing it.

### 2026-10-12 - The CVE assessed, and the audit wired into `gates` as an explicit accepted risk

Full evidence: `docs/evidence/cve_reachability_and_gate.txt`. 21 tests.

**What it is: remote code execution.** `fsspec` `ReferenceFileSystem` evaluates Kerchunk reference
documents through **unrestricted `jinja2.Template(...).render(...)`** -- a document from an
attacker-controlled URL can execute Python when the filesystem is opened. That is the most serious
class of finding, which is why this round did not stop at "a CVE exists".

**Reachability: assessed, and it is unreachable.** The vulnerability needs a `reference://` URL backed
by a Kerchunk document. **Zero references to `ReferenceFileSystem`, `reference://` or kerchunk exist
in TigerQuant -- and zero across the entire installed engine.** The class is never instantiated and
the protocol is never used, by either side. The project also does not invoke nautilus's
`backtest/__main__.py` or `live/__main__.py`, the only places that call `fsspec.open` at all.

**The deeper failure was not the CVE.** It was that the audit was recorded as "cannot run" **once and
never retested**, carrying a gate item the whole time. **A check that lives in someone's memory rots
exactly the way a documented test count does** -- a lesson this project has now learned twice.

**So the audit is part of `gates`.** `--record-audit` runs `pip-audit` and records it; every later
run reports it fast and offline. **No record is a FAILURE, not a pass** -- an audit that has never run
is not a passing audit, and that was precisely the state here. Verified by removing the record:
`gates` exits 1 with `DEPENDENCY AUDIT NEVER RUN`.

**And my own first version had a defect, found by running it.** It marked the finding "fixable"
because `pip-audit` reports `fix: 2026.6.0` -- but it is **not fixable here**, established last round
by trying it. **The gate would have failed for ever with no action that could satisfy it, and a gate
that fails on the unactionable gets disabled.** The check now distinguishes `upstream_fix_exists`
(what pip-audit knows) from `needs_action` (what somebody can do).

The accepted risk is a **literal table in reviewed code, not a config file** -- accepting a known
vulnerability should require editing code somebody reviews. Every entry must state why it cannot be
acted on **and** `REVIEW WHEN`, enforced by three tests. **That is the difference between an accepted
risk and an ignored one.**

**Two tests failed when this was first wired, and they were RIGHT to:** a `GateReport` with no audit
is genuinely not ok. They now supply a recorded audit -- the correct fixture, not a workaround.

### 2026-10-12 - CI was running the whole time, and red on every push

Full evidence: `docs/evidence/ci_was_red.txt`. 10 tests. Found by sweeping the limitations recorded
in earlier rounds -- work this project had been naming as "the obvious next thing" for two rounds.

**The stale claim.** `WORK_QUEUE.md` recorded, and PROGRESS.md repeated: *"No remote CI runner...
no GitHub run exists to link."* **That was false.** The repository had **32 completed workflow runs**,
the most recent five timestamped `03:58`, `04:08`, `04:16`, `04:25`, `04:40` -- **the exact moments
this project was pushing.** Every one failed.

`github.com` was unreachable in the early rounds and became reachable later. **The limitation was
never retested against the new network.** This is the second time in three rounds a "cannot" claim
turned out to be stale, and the first (`pip-audit`) was also carrying a gate item.

**Why nobody saw it: two stale claims covering for each other.** The project believed there was no CI,
so it never looked; and because it never looked, nothing contradicted the belief. **A green local run
confirmed the belief every time without testing it.** A limitation believed is a check not performed,
and its absence looks exactly like success.

**What it was failing on.** `mypy` strict, on every platform and version:
`src/tigerquant/db/postgres.py:50: error: Cannot find implementation or library` -- that line is
`import psycopg`, inside a `try/except ImportError`.

**The code was right.** ADR-0001 §6 requires optional integrations to import lazily and degrade
explicitly. **The configuration was wrong:** the mypy override list read
`["tigeropen.*", "nautilus_trader.*", "litellm.*"]` -- **`psycopg.*` was missing**, while the other
three optional SDKs were present. CI's fast job syncs only `--extra dev`, so `psycopg` is genuinely
absent there; **locally the `db` extra is installed, so it passed.** Reproducible only in the
environment nobody was looking at.

**The same defect shape, a fourth time:** a list on one side of an interface disagreeing with a list
on the other, with nothing comparing them. The first three were found by reading. **This one was
found by CI, and only after CI was discovered to exist** -- had the claim been believed one round
longer, those would still be the only three known.

**Fixed, with a check that stops it recurring:** every extra is either mapped to its import root or
**named with a reason**, asserted in BOTH directions, plus a test that the `psycopg` import remains
guarded -- because the override hides a missing MODULE from mypy and must not become cover for an
unguarded import.

**`Docker Hub IS still unreachable`** (retested: `registry-1.docker.io` and `auth.docker.io` both
time out), though `ghcr.io` and `public.ecr.aws` are reachable, so the stated claim is true while the
implied one is narrower than it sounds.

**Named limitation:** `mypy` passing locally is NOT evidence it passes in CI -- that is the whole
lesson. The new test compares DECLARATIONS; it cannot prove CI is green. The verification is the CI
run this commit triggers.

### 2026-10-13 - 105 safety-critical tests were running in NO CI lane

Full evidence: `docs/evidence/ci_broker_lane_gap.txt`. 14 tests.

**The gap.** The extras job installs EVERY optional extra and then selected:

    -m "requires_engine or requires_llm" \
      || test $? -eq 5   # exit code 5 == "no tests collected yet" (CR-04/CR-06 add them)

Measured: `requires_broker` -> **105 tests**, `requires_engine` -> 31, `requires_llm` -> 0.
**105 tests were run by nothing in CI**, and they are the ones that matter most:
`test_guard_write_scope.py` and `test_tiger_execution.py` cover `TigerAccountGuard` -- **the only
thing keeping this project paper-only**, because the CR-02 OAuth grant returns a live-capable
STANDARD account alongside the paper account.

**The guard that prevents live trading had never been verified by CI**, while the repository's
documents described it as covered by tests.

**The escape hatch hid it.** `|| test $? -eq 5` makes the step PASS when pytest collects nothing. It
was honest when written -- CR-04/CR-06 had no tests then -- and became a way for a regression that
broke the markers to pass silently. Removed, because 136 > 0 was measured rather than assumed.

**The fix**: `-m "requires_broker or requires_engine or requires_llm"` -> **136 passed** locally.

**A durable check, and it was VACUOUS TWICE.** `test_ci_lane_coverage.py` asserts every declared
marker is selected by some lane. Both false passes were found by REVERTING the fix and demanding the
check fail:

1. **It grepped prose.** It searched run bodies for the marker NAME and matched `requires_broker`
   inside the comment I had just written explaining that `requires_broker` was missing. **8 passed**
   against a broken workflow.
2. **It counted a DESELECTION as a selection.** After parsing `-m` expressions, it still passed: the
   fast lane excludes with `not requires_broker`, and a naive token scan found it there. **14 passed**
   against a workflow with the fix removed.

The parser now handles negation explicitly, and a test asserts the parser itself works -- because a
broken parser makes every check below it vacuous. **Reading either version would have shown something
that looked correct.**

**Named limitations:** `requires_llm` selects ZERO tests, so the check cannot tell "selected and
populated" from "selected and empty". The extras lane is ubuntu-latest only. And `requires_db` still
runs in no CI lane -- deselected in the fast lane, with nothing starting a PostgreSQL service, so 20
tests are covered only locally.

### 2026-10-13 - CR-06: the LiteLLM binding, and a marker that was worn by nothing

Full evidence: `docs/evidence/cr06_llm_transport_contract.txt`. 14 tests.

**The gap.** `requires_llm` was **declared in pyproject, selected by a CI lane, and worn by no test at
all.** The only thing verified about the `llm` extra was an import-compatibility check --
`hasattr(litellm, "completion")` -- which says the package is present and **nothing about the adapter
that talks to a provider.**

**14 tests, all offline.** No key, no network, no provider. They assert the properties the module's
own docstring *claims*:

- **a missing key raises BEFORE the network** -- asserting `not capture.called` as well as the
  exception, because "raised after calling out" satisfies the letter and defeats the point;
- the message says **the key is never read from a file**, the property keeping credentials out of
  this repository;
- **`num_retries=0`** -- a retry makes latency unbounded and hides an outage behind a slower failure;
- the timeout and token budget are passed through;
- **a provider error is redacted** -- a provider message can echo a URL carrying a token;
- the response is normalised to a plain mapping, so the decision layer never sees a vendor type.

**The redaction test is PROVED**: disabling `redact` makes it fail (14 passed -> 1 failed).

**Verified locally against a stub, and the limit of that is named.** `uv sync --extra llm` fails here
with a network error, so litellm exists only in CI. The stub exercises every branch because the
transport imports litellm *inside* the function -- but **a stub accepts any keyword arguments**, so
only CI could settle whether the real `litellm.completion` does. It did: the extras lane passed.

**And the commit that added this work cited this evidence file before it existed.** A commit pointing
at evidence that is not there is worse than one citing none, because it stops the reader looking while
appearing to have shown them. That prompted the check below.

### 2026-10-13 - CR-09: in-process hang detection, and the check that closes how it went wrong

Full evidence: `docs/evidence/cr09_hang_detection.txt`. 41 tests.

**The gap, in the module's own words.** `supervisor.py`'s docstring states the problem -- *"a single
write at the end cannot distinguish 'working' from 'stuck in cycle 41'"* -- and **nothing implemented
the distinction.** The heartbeat carried everything needed and no reader used it.

**The signal, and why writing twice was the right design.** The heartbeat is written at the START of a
cycle (`running`) and again at the END (`idle`/`backoff`). **So during a long cycle the phase stays
`running` and `written_at` stops advancing.** That is the whole detector -- no second thread, no timeout
wrapper. The original design was correct and worth stating as the reason this is cheap.

**Only `running` can hang.** A long `backoff` is the mechanism WORKING; long `idle` is a supervisor
between cycles. **A detector that cries wolf gets turned off** -- the same reasoning as PARTIAL gate
cases and the accepted CVE.

**Wired, not merely computed.** `supervise` reads the previous heartbeat BEFORE the first write
overwrites it, and that moment is the only chance: **restarting a supervisor that HUNG is otherwise
indistinguishable from restarting one that stopped cleanly.** Silent when there is no heartbeat, because
a line printed every time is a line ignored.

**THREE CI FAILURES IN A ROW, each an unenumerated case.** (1) A hand-built `Namespace` missing the new
flag -- five tests failed, found by tests OTHER than the one written for the flag. (2) The fixture
fixed, and **a real bug surfaced**. (3) **`heartbeat=None`** -- a supported configuration, and
`read_heartbeat(None)` raised. **That one was the product.**

**My own test had predicted (1) and did not prevent it.** It checked the PARSER; the failure was in a
FIXTURE. **Naming a risk is not covering it.**

So `test_fixture_mirrors_command.py` now compares the parser's surface against the fixture's in BOTH
directions, with the **extractor tested itself** -- because a broken extractor makes both comparisons
vacuous, which this project has hit three times. Proved: removing `hang_after` -> 1 failed (caught).

**None of the three was caught locally.** The sandbox cannot create a temp directory, so the tests
needing one do not run here. **A green local run is WEAK evidence**, and this round is the clearest
demonstration of that so far.

**Named limitations:** the 900s bound is judgement, not measurement; the detector reports on RESTART
only, so nothing watches while the process runs; a hang is not escalated and nothing halts; and the
fixture check covers one command, with the rest to be added by hand.

### 2026-10-13 - Three rounds that shipped without a record, now recorded

**THIS ENTRY EXISTS BECAUSE THE WORK IT DESCRIBES WAS NOT RECORDED.** Backoff jitter, the watchdog
escalation and evidence-fact projection all shipped code, tests and green CI -- and **no evidence file
and no PROGRESS entry.** The objective requires recording evidence for each CR, and a session spent
hunting claims nothing verifies had produced three rounds of exactly that.

Found by checking whether recent commits cited evidence: **only one of the last five did.** The check
added alongside this now catches the reverse direction too -- an evidence file nobody cites.

**Backoff jitter** (`docs/evidence/cr09_backoff_jitter.txt`). `delay_for` was a pure function of the
attempt number, so **every client that saw the same outage retried at the same instants** -- a
thundering herd against a service only just coming back. The jitter is DERIVED from a hash rather than
drawn from `random`, because the journal must replay identically. **Downward only**, so the ceiling
keeps its meaning. On by default, because an opt-in mitigation is one nobody enables.

**Watchdog escalation** (`docs/evidence/cr07_supervisor_stall_escalation.txt`). A hung supervisor now
reaches the halt path, not just the console. **The watchdog takes a VERDICT, not a path** -- it is a
deterministic evaluator and reading a file is a side effect. `HUNG` is CRITICAL; `STALE` and `UNKNOWN`
warn, because an absent heartbeat is a first run as easily as a fault. **`None` means NO CHECK WAS
PERFORMED**, deliberately distinct from `STALE`.

**Evidence-fact projection** (`docs/evidence/cr09_evidence_projection.txt`). `evidence_facts` was
declared, both repositories implemented writing, and **nothing called either** -- a table that is never
populated looks exactly like a table that works. The event carries the Fact's OWN JSON so the projector
rebuilds with `model_validate`, rather than a hand-written field list that would be a second definition
of `Fact`. **This was reverted once** (`docs/evidence/cr09_evidence_projection_attempt.txt`) after its
tests would not go green, then completed once five interfaces were looked up instead of guessed.

**The pattern worth naming:** the revert cost a round that one command per signature would have
prevented, and the missing records cost three rounds of legibility that no test was watching.

### Evidence index - files not cited by a round entry

**THESE HAD NO PATH TO THEM FROM ANY DOCUMENT.** An evidence file nobody cites is indistinguishable
from one that was never written, except that finding out costs the reader the effort. The
`test_evidence_orphans.py` check now fails on any file in `docs/evidence/` that no document mentions,
and this index is how these are mentioned.

Most are records of work that was themselves superseded or reverted, kept because a reverted attempt is
evidence too -- of what was tried and why it stopped.

- `docs/evidence/ci_green.txt`
- `docs/evidence/ci_status_after_fixes.txt`
- `docs/evidence/count_invocation_keying.txt`
- `docs/evidence/gate_counts_attempt_reverted.txt`
- `docs/evidence/ops_shutdown_push_incident.txt`

### 2026-10-13 - A sweep for dead code, testing the claim that offline work was exhausted

Full evidence: \docs/evidence/dead_code_sweep.txt\.

**A claim nothing verifies is the failure this session has been about**, so last round's "offline work
is close to exhausted" was tested rather than asserted. The sweep is the systematic version of a pattern
found FOUR TIMES BY ACCIDENT: \evidence_facts\, \KNOWN_CATEGORIES\, the pipeline's category mapping, and
\dd_fact\/\acts_as_of\ -- each a thing that LOOKED wired and was not.

    400 public definitions scanned
     43 referenced NOWHERE   <- NOISE: mostly return-type dataclasses
      8 TRULY DEAD

**The 43 was noise and saying so matters.** \IngestionReport\ is the return type of \ingest()\ and lives
inside \connectors.py\, so a cross-file count calls it unused when it is central. **A finding-shaped
number attached to nothing is worse than no sweep.**

**Two of the eight are not housekeeping.** \write_report\ in \pipeline/long_run.py\ has the docstring
*"Persist a stability report as evidence"* and **NOTHING CALLS IT** -- the same shape as
\evidence_facts\. \empty_or_unfilled\ in \execution/parity.py\ may indicate a MISSING PARITY CASE rather
than dead code. **Neither is closed; both are recorded.**

**The measurement changed what it measured.** The test names every entry in \KNOWN_DEAD\, so counting
itself made each "referenced in tests" and the sweep reported nothing dead -- nine tests failed while
the standalone script passed. **An observer that changes the observed is indistinguishable from a
working check that finds nothing.** It also took 120s; \lru_cache\ brought it to 11s, because **a check
slow enough to notice is a check somebody deletes.**

**And the orphan check written last round caught this very file** in CI, because the evidence was written
before it was cited -- which is precisely what it exists to detect.

### 2026-10-13 - CR-09's long-run evidence is now reproducible from the repository

**THE FINDING BEHIND LAST ROUND'S DEAD-CODE ENTRY.** The sweep flagged `write_report` -- docstring
*"Persist a stability report as evidence"*, called by nothing. Looking further: **`run_long_sessions`
was called by nothing either.** The whole long-run apparatus was exercised only by its tests.

**So CR-09's long-run evidence was NOT REPRODUCIBLE.** The apparatus existed, the report type existed,
the function that persists it existed, and no command ran them. Regenerating `cr09_long_run.txt` meant
knowing how to call two functions nothing exposed.

`tools/long_run_evidence.py` closes it, and **running it reproduces the documented figures exactly**:

    5 sessions x 390 bars
    simulated span    32.50 hours     <- CR-09 documents "32.5h of market time"
    events            8632           <- CR-09 documents "8632 events"
    verdict           STABLE
    risk denials      275

**A TOOL RATHER THAN A FIXTURE**, because it produces EVIDENCE rather than asserting a property: it
belongs beside `tools/db_migrate.py`, run deliberately and recorded, not on every commit.

**And it had to be made to work INSIDE this sandbox.** The first run failed writing its fixture to a
temporary directory, which the DSH policy denies -- so the tool now takes a workdir in the workspace.
**A tool that only works outside the environment it is run in is not evidence.**

The dead-code list loses its `write_report` entry, and the closure is asserted **from the call site**
rather than from the list, so removing the wiring fails the test. A list entry could not detect that.

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

- **CR-04 is partial.** The engine is installed and verified, the mode table, interface contract,
  guarded `ExecutionClient` and parity harness are all done. What is missing is *strategy-loop*
  parity — running a Nautilus node through the same scenario the internal simulator runs — which
  needs a kernel, a data client and a strategy registration. `DataClient` and
  `InstrumentProvider` are pinned by the compatibility test but not written, because without a
  verified market-data entitlement there is nothing for them to load.
- **CR-03's remaining precondition is a decision, not code.** The guard is hardened and
  tested; the write-scoped grant is the owner's call (docs/evidence/guard_write_scope.txt).
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

1. **CR-09 continues.** Drift monitoring is in place; what remains is a supervised
   long-running process, alert routing (there is no pager or webhook), long-duration
   paper/shadow runs, latency and P&L attribution reporting, and the incident runbooks.
2. **CR-03's last acceptance item is blocked on the owner**, not on code: a real paper round trip
   needs a write-scoped OAuth grant (`api.trade:write`).
3. CR-04's engine half needs the wheel to install; the mode table and parity harness are done.
4. CR-05's remaining gap: news/filing ingestion connectors.
