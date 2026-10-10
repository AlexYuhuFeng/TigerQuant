# Live-readiness review — **NOT an activation**

**Status: this document activates nothing.** It does not describe a procedure for an agent to
turn live trading on, and no agent may do so. It is the checklist a **human** would have to work
through before live trading could even be *considered*, together with an honest statement of what
is not ready.

Read the first two sections before the rest. They are the reason this document exists.

---

## 1. What is actually true today

| | |
|---|---|
| Live trading | **OFF.** No code path can arm it. `Mode.LIVE` raises in the engine adapter; `capabilities_for(Mode.LIVE)` reports `can_submit_orders=False` without an explicit human authorization value. |
| Live credentials | **None held.** The project's OAuth grant carries four read-only scopes. It could not place an order even if every check above it were removed. |
| Broker connectivity | Tiger **paper** account, verified from the broker's own response. Read-only. |
| Orders ever submitted | **Zero.** Not one, in any mode. |
| Implementation status | `SIMULATED`, except authorization and account discovery, which are `PAPER_VERIFIED`. |
| Risk limits | Enforced, and the enforcement is tested against bypass attempts. Never relaxed. |

`tigerquant doctor` prints this state and fails if the labelling drifts. Two of its checks are
"determinism (no wall-clock calls outside core.clock)" and "no live enablement surface in
source", so a change that quietly introduced either would fail the build rather than the review.

## 2. Why paper success would not be sufficient

The acceptance gates say it directly: *"Paper success is necessary but not sufficient."* Concretely,
a paper account differs from a funded one in at least these ways, and none of them is fixed by
more paper trading:

- **No queue position.** A paper fill is immediate and at a modelled price. A real order joins a
  book, and where it sits determines whether it fills at all.
- **No market impact.** Paper fills never move the price. Size that looks free in simulation is
  not free.
- **No partial-fill reality.** Paper fills whole orders. Real ones fill in pieces, sometimes
  days apart, and sometimes not at all.
- **No rejections for real reasons.** Buying power, locates, halts, corporate actions and
  venue-specific rules all reject orders that paper accepts.
- **No latency.** The whole path — model, risk, adapter, venue — is fast in simulation because
  the slow parts are absent.
- **Different data.** A paper environment's quotes are indicative. Entitlements and feed
  behaviour are a separate purchase and a separate failure mode (still unverified in this
  project).

Every one of those is a reason a *backtest or paper* result overstates what live trading would
do. None of them is a reason to skip the checklist; all of them are reasons the checklist exists.

## 3. Hard preconditions — each one blocks, none is waivable by an agent

Each row must be completed by a **named human**, with evidence recorded, before the next row is
attempted. An agent may prepare evidence for a row; it may not mark one done.

| # | Precondition | Evidence required | Status |
|---|---|---|---|
| 1 | Independent security review of the adapter, credential handling and the read-only scope ceiling | Written review, reviewer named, scope stated | **Not done** |
| 2 | Broker-account compliance check for the specific account and jurisdiction | Broker or counsel confirmation | **Not done** |
| 3 | Market-data entitlement review: which feeds, which licences, which redistribution rights | Vendor confirmation per feed | **Not done** — entitlements are currently *unverified*, and this is the largest gap |
| 4 | Capital and loss-tolerance sign-off: maximum capital, maximum daily loss, maximum drawdown, in writing | Signed statement with figures | **Not done** |
| 5 | Operational-risk assessment: who watches it, what they do at 03:00, how they reach the kill switch | Runbook with named on-call | **Not done** |
| 6 | Model provider qualification for the exact pinned version, with a benchmark report | `benchmark_provider` output for that version | **Not done** — no provider has been called at all |
| 7 | Successful long-duration paper and shadow runs with no unexplained divergence | Journal, drift report, incident list | **Not done** — blocked on a write-scoped grant |
| 8 | Reconciliation proven against a real venue, not the fake broker | Reconciliation report from a live paper session | **Not done** — same blocker |
| 9 | Independent review of the risk policy *values*, not just the mechanism | Reviewer's signed parameter set | **Not done** — the defaults are engineering judgement, not a risk decision |
| 10 | A written decision to enable live, by the account owner, recorded outside this repository | Dated decision | **Not done** |

**Rows 3 and 9 deserve emphasis.** Neither is a coding problem, and both would be missed by an
implementation-focused review. Entitlements are a commercial fact. Risk *values* — 2% daily loss,
5% drawdown, 2× turnover — are choices this project made to have *something* defensible in place;
they are not calibrated to anyone's capital or tolerance.

## 4. Isolated runtime plan (a design, not a deployment)

The invariant is that modes must not share anything that could leak. The design is per-mode
isolation at every layer, and the parts already built are marked.

| Layer | Isolation | State |
|---|---|---|
| Database | One database **and** one role per mode; the server refuses cross-mode CONNECT | **Built and verified** — `tq_backtest` is denied `CONNECT` to `tq_paper` |
| Journal | One JSONL file per mode, hash-chained, mode stamped on every envelope | **Built** |
| Credentials | One token directory per mode; `.secrets/` git-ignored and scanned before every commit | **Built** |
| Account identity | The verified account number is checked against the broker's own response on every snapshot | **Built** |
| Order namespace | `client_order_id` carries the mode prefix; the projection is per-mode | **Built** |
| Process | One worker per mode, with a mandatory-expiry lease and recorded takeovers | **Built** — `PostgresLease` |
| Deployment | Separate containers, separate secrets, separate service accounts | **Not built** — no container for the worker exists yet |
| Alerting | Per-mode routing so a paper alert cannot page for a live incident | **Not built** |

The last two are the honest gaps. A live deployment needs them and does not have them.

## 5. Remaining failure modes, ranked by how badly they would hurt

These are the ways this system could lose money that are **not** covered by the current tests.
Listed honestly, worst first.

1. **A strategy that is wrong in a way that is profitable in backtest.** The widest class by far.
   Overfitting, survivorship bias and lookahead are statistical failures, not engineering ones,
   and no amount of test coverage detects them. CR-05's point-in-time guards and the leakage
   checks reduce the *mechanical* forms; none of them tells you the edge is real.
2. **A model that degrades silently.** The provider path rejects a changed *version*, but a
   provider can change behaviour behind a stable version string. Detecting that needs a
   longitudinal benchmark, which does not exist.
3. **Market-data staleness that the staleness policy does not model.** Per-category horizons are
   in place, but a feed that is *present and wrong* passes every freshness check.
4. **A halt that does not halt.** The kill switch is tested in-process. Nobody has tested it
   against a wedged worker, a full disk, or a database that has gone away mid-cycle.
5. **Reconciliation that cannot complete.** The blocking-intent gate stops new exposure, which is
   correct, but there is no documented procedure for a human to resolve a stuck intent.
6. **The paper-versus-live gap itself** (section 2). Every item there is unhedged.
7. **Provider or venue outage at the wrong moment.** The paths abstain rather than retry, which
   is safe, but "safe" here means "does nothing while the market moves".
8. **Correlated failure of the risk layer.** The gateway is one process. If it is dead, the
   pipeline cannot submit — fail-closed — but nothing alerts that it is dead. There is no
   heartbeat.

Items 4 and 8 are the ones a long-duration run would most likely surface, which is an argument
for finishing CR-09's supervised long-running process before anything else.

## 6. Hard boundaries that survive every future change

These are not preconditions to be satisfied and then forgotten. They are properties of the
project, and a change that violates one is a defect regardless of what it enables.

1. No coding agent may enable live mode, link a live account, or fund an account.
2. No live credentials, private keys or account numbers in Git, logs, prompts, screenshots or
   audit outputs. Enforced by a scan before every commit.
3. Only the authorized execution service may submit orders, and only with a single-use risk
   approval. The model has no tool through which an order could be reached.
4. No evidence means no trade. Unresolvable or contradictory evidence produces abstention.
5. The model never supplies final numerical truth; position size, exposure and marks are
   recomputed deterministically.
6. Risk limits may be tightened by anyone, and relaxed only by a human, with a versioned record.
   An agent never relaxes one.
7. Missing functionality is labelled `SIMULATED` or `PAPER_VERIFIED`. Nothing is presented as
   production-ready that has not been run against the thing it claims to talk to.

## 7. What an agent may and may not do about all of this

**May:** prepare evidence for any precondition; build the isolated-runtime pieces in section 4;
write and run tests that reduce the failure modes in section 5; keep this document current;
refuse work that would cross section 6.

**May not:** mark any precondition complete; enable live mode; request live credentials; relax a
risk limit; present a paper or simulated result as live-verified.

If asked to do any of the second list, the correct response is to decline and say why — and that
refusal is the control working, not an agent being unhelpful.
