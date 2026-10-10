# Verification requirements

## Continuous integration
- Lock dependencies with hashes or a resolved lockfile. Tests run in the pinned matrix; no floating production dependencies.
- Unit tests, contract tests, static typing/linting, package dependency security checks and secret scans.
- Integration tests must use a fake broker by default and a separate explicit Tiger PAPER test profile.

## Critical test cases
1. Paper account is 17-digit Tiger paper type and reported `PAPER`; mistakenly configured live account refuses all order calls in non-LIVE environments even when ID type is forged.
2. The LLM cannot call, import or access `TradeClient.place_order()` through its tool boundary.
3. Loss-limit breach / price stale / order mismatch / missing quote -> fail closed and log reason.
4. Order request times out after broker acceptance: no blind retry; reconciliation recovers status or quarantines.
5. Partial fill then cancel: remaining quantity and realized inventory match broker records.
6. Process restarts with resting orders: startup reconstruction before new exposure.
7. Duplicate strategy callbacks and duplicate API push events do not double-submit orders or book fills twice.
8. Two instances competing for active worker lease: only one execution owner can submit.
9. Contradictory news or unsupported fact IDs => NO_TRADE; prompt injection in news is ignored.
10. Model returns well-formed JSON but incorrect arithmetic: deterministic recomputation rejects.
11. Model provider outage, high latency, invalid output or changed provider version never bypasses risk.
12. Historical point-in-time replay cannot see future revisions, publication dates or datasets outside event horizon.
13. Paper credentials and event data cannot reach live workers; separate account routing and secret namespaces.
14. Market is closed, halted or beyond approved order window: reject unless documented compatible policy.
15. Restart, reconnection, database outage and clock skew generate clear alerts and no new exposure.
16. Risk configuration cannot be changed through the AI decision API or an unauthenticated dashboard action.

## Release evidence per CR
- Version-pinned dependency inventory, exact test command and result.
- Code diff, risk impact, integration-test log and known limitations.
- Evidence link to stored replay fixtures and corresponding failure-injection report.
- Updated progress/backlog state and next work item.

## Live gate
No coding agent may enable live mode. A human must separately conduct provider qualification, market-data entitlement review, broker-account compliance check, capitalization and independent operational-risk assessment. Paper success is necessary but not sufficient.
