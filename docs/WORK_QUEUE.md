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
