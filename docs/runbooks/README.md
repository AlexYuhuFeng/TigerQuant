# Incident runbooks

CR-09's gate asks for incident runbooks. These are written for someone woken up, not for someone
reading for pleasure: **what you will see**, **what to do**, and **what NOT to do**.

## Read this first

**Every runbook here is a containment procedure.** None of them restores service automatically,
and none of them is a fix. The system is designed so that the safe response to almost every
incident is *stop opening exposure and look*, and most of these runbooks are that instruction
made specific.

**The system never repairs itself.** That is deliberate. A drift between the journal and the
database, a reconciliation mismatch, or an unresolved order all mean we do not know which record
is right. Automatically picking one would destroy the evidence needed to find out.

**Nobody can activate live trading from a runbook.** That is not an incident response, and
`docs/LIVE_READINESS.md` says what it would take.

---

## How to tell what is happening

```powershell
python -m tigerquant doctor                     # modes, determinism, safety checks
python -m tigerquant verify-journal <journal>   # is the hash chain intact?
python -m tigerquant drift <journal> --mode M   # do journal and database agree?
python -m tigerquant metrics <journal> --mode M # P&L attribution and activity counts
python -m tigerquant report <journal> --mode M  # full decision/execution lineage
python -m tigerquant dashboard <journal> --mode M   # the same, browsable
```

Exit codes are meaningful and intended to be used by a script:

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `verify-journal` | chain intact | chain broken | — |
| `drift` | consistent | **drifted** | database unreachable |
| `report` | lineage clean | needs attention | — |
| `metrics` | attribution reconciles | **does not reconcile** | — |

---

## RB-01 — The drift monitor reports drift

**You will see.** `tigerquant drift` exits 1, and an `AlertRaised` appears in the journal with
code `UNRECONCILED`. The monitor alerts once on discovery, once on escalation (after 3
consecutive drifted checks), then at most once per 15 minutes. **One alert means one problem**,
however many times you see it.

**What it means.** The journal and the database disagree. One of them is wrong and the system
does not know which.

**Do this.**

1. Read the drift report's detail line. It names the direction:
   - *"journal event(s) absent from the database"* — the journal has things the database does not.
     Usually a projection that did not run, or a transaction that rolled back.
   - *"database event(s) absent from the journal"* — **this is the more alarming direction.** The
     database holds history the journal does not, which means either a journal file was truncated
     or something wrote to the database directly.
2. Get the lineage: `python -m tigerquant report <journal> --mode M`. Check `unlinked_orders` and
   `unlinked_fills`.
3. Check whether the drift blocks exposure. `blocks_new_exposure: true` in the report means new
   exposure should already be halted by the watchdog.

**Do NOT** run `tigerquant project` to "fix" it before capturing evidence. Projecting writes the
journal's version over the database's, which is correct *if* the database is the one that is
behind — and destroys the evidence if it is not. Copy both aside first.

**Then.** Re-project only once you have decided the journal is authoritative and intact
(`verify-journal` returns 0). Re-check `drift`; it should exit 0 and the recovery alert will say
so.

---

## RB-02 — An order is stuck

**You will see.** `blocking_intents > 0`, so the risk gateway denies every new order with
`UNRECONCILED`. This is the gate working: an unresolved order blocks new exposure by design.

**What it means.** An intent is in `SUBMIT_UNKNOWN`, `QUARANTINED`, partially filled, or
cancelling, and the execution service will not open a second position while the first is
unresolved.

**Do this.**

1. `python -m tigerquant report <journal> --mode M` and find the intent. The `states` line shows
   exactly where it stopped.
2. `SUBMIT_UNKNOWN` means the submission may or may not have reached the venue. **It is never
   retried**, on purpose: retrying a submission whose outcome is unknown is how duplicate
   positions happen.
3. Compare against the venue: `python tools/tiger_reconcile.py`. If the order exists there, the
   outcome is knowable; if it does not, the submission did not land.
4. Resolve it in the journal, then re-check.

**Do NOT** resubmit. Do NOT delete the intent. Do NOT relax the blocking-intent check — it is the
control that stops a lost order from becoming a second, unintended position.

---

## RB-03 — The hash chain is broken

**You will see.** `verify-journal` exits 1 with `first_error` and `first_error_seq`.

**What it means.** A record was altered, a line was lost, or two writers appended concurrently.
The journal is no longer trustworthy as a record of authority, and **every projection built from
it is suspect.**

**Do this.**

1. **Stop.** Do not project. Do not repair. Copy the file aside, read-only.
2. Note `first_error_seq`. Everything before it is intact and verifiable; everything from it
   onwards is not.
3. Treat the last good sequence as the end of trustworthy history.
4. Escalate: a broken chain is either corruption or an unauthorised write, and the difference
   matters.

**Do NOT** regenerate the chain from the database. The database was built *from* the journal, so
rebuilding the journal from it would launder a corruption into a consistent-looking history.

---

## RB-04 — The watchdog has halted

**You will see.** The risk gateway denies everything with `RISK_HALTED`, and `RiskStateChanged`
in the journal names the reason code.

**What it means.** One of: daily loss limit, drawdown limit, turnover limit, unresolved intents,
unreconciled account, unreachable broker, or clock skew. **The halt is doing its job.**

**Do this.**

1. Read the reason code. Each maps to a specific condition, and each is a real one.
2. `python -m tigerquant metrics <journal> --mode M` shows the figures the limits were measured
   against.
3. **Cancellation still works while halted.** The halt gates *new exposure* only. Reducing risk is
   always permitted — that is deliberate, and a risk control that could block a cancel would be
   dangerous.
4. Clearing the halt requires `Actor.OWNER`. An agent cannot do it, and neither can the watchdog
   that set it.

**Do NOT** clear the halt to "see if it happens again". Diagnose first: the limit exists because
something crossed it.

---

## RB-05 — The broker is unreachable

**You will see.** The watchdog reports unreachable, or reads fail with a transport error.

**What it means.** We cannot see our own positions. Any decision made now would be made blind.

**Do this.**

1. The system already refuses new exposure. Confirm it has.
2. Try a read-only check: `python tools/tiger_reconcile.py`. If that fails too, it is the
   connection rather than the credentials.
3. If the token expired, re-authorisation is an **owner** action in a browser. No code path can
   refresh a grant on its own.

**Do NOT** retry a submission during an outage. A timeout after transmission is `UNKNOWN`, not
failed, and the difference matters.

---

## RB-06 — The P&L attribution does not reconcile

**You will see.** `tigerquant metrics` exits 1 and prints a `RESIDUAL`.

**What it means.** The attributed parts do not add up to the ledger's movement. The usual cause is
benign and worth knowing: `PositionChanged` carries the mark that applied when the position
changed, while `LedgerMarked` keeps being emitted as the mark moves — so the last position
snapshot can be from an earlier instant than the final valuation.

**Do this.**

1. Check the size of the residual against the instrument's price movement over the session.
2. Check `breakdown_agrees` in the JSON record. If the per-instrument breakdown disagrees with
   the aggregate, that is a different and quieter bug — investigate that first.
3. If the residual is larger than a mark move can explain, treat it as a ledger or attribution
   defect and stop.

---

## RB-07 — The model provider fails

**You will see.** Decisions stop arriving, or every decision is `NO_TRADE` with a
`provider_failure` reason.

**What it means.** An outage, a timeout, unparseable output, or **a different model version than
the one approved**. All four produce an abstention, never a trade, and that is by design
(acceptance gate 11).

**Do this.**

1. Read `provider_failure`: `MODEL_UNAVAILABLE`, `MODEL_OUTPUT_INVALID`, or
   `MODEL_VERSION_UNPINNED`.
2. `MODEL_VERSION_UNPINNED` means the provider answered from a different model than configured.
   **Do not accept it** — the recorded `model_version` would become a lie. Re-pin deliberately
   and re-run the benchmark.
3. An abstention is a correct outcome. There is nothing to fix if the market simply does not
   offer a trade.

---

## RB-08 — Someone asks for live trading

**You will see.** A request — from a person, a prompt or a ticket — to enable live mode, add a
live credential, raise a risk limit, or "just test one real order".

**What it means.** This is not an incident. It is the boundary.

**Do this.** Decline. Point at `docs/LIVE_READINESS.md`. The ten preconditions there each require a
**named human** and written evidence, and an agent may prepare evidence for any of them but may
mark none of them complete.

**Do NOT** rationalise it as a small step. Live trading is not gated because it is technically
hard; it is gated because the failure modes in section 5 of that document are not covered by any
test in this repository.
