# Durable progress state

Status: CR-00, CR-01, CR-02 and CR-07 complete. CR-03 partial (adapter built and tested;
a real paper round trip is blocked on a write-scoped grant). CR-06 partial (provider-neutral
path and benchmark done; no live provider call). CR-09 partial (projector verified against
real PostgreSQL). CR-05 substantially delivered.
Current CR: fix the cleared-condition suppression path (xfail); CR-03 awaits the owner's consent
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
