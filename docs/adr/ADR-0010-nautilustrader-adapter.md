# ADR-0010 — NautilusTrader integration as an external adapter

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (adapter built in CR-04)

## Context

ARCHITECTURE.md mandates implementing `InstrumentProvider`, `DataClient` and `ExecutionClient` as an *external adapter package* using the supported stable API, and requires that the engine's cache/database persistence not be treated as our independent audit store. AGENTS.md invariant 12 forbids guessing SDK interfaces.

Version selected: **`nautilus_trader==1.231.0`** (`requires_python >=3.12,<3.15`, cp314 Windows wheel published — evidence `docs/evidence/cr00_pypi_probe.json`). Version-specific API details are recorded in `docs/integrations/nautilus_api_notes.md` with citations and are re-verified by the compatibility test in CR-04; anything unverified is marked UNVERIFIED there and may not be relied upon in code.

**Documentation hazard (verified 2026-10-10).** `https://nautilustrader.io/docs/latest/` documents the v2.0.0rcN Rust-core line, whose adapter interface differs from 1.231.0, and there is no versioned `/docs/1.231.0/` path — only `latest` and `nightly`. Even `docs/developer_guide/adapters.md` *inside the v1.231.0 tag* is already v2-oriented. The adapter must therefore be written against the pinned tag's source tree, not against any published guide, and guarded by an interface-shape test so a wheel bump fails loudly instead of drifting silently.

**Attribution note.** No official v1 statement was found asserting that the engine's persistence is not an audit store. That is *our* engineering conclusion (ADR-0004), not a Nautilus-documented caveat, and it must not be cited as one.

**No PAPER environment (verified).** `Environment` is `{BACKTEST, SANDBOX, LIVE}`. NautilusTrader cannot express our PAPER mode: a real Tiger paper account is `Environment.LIVE` plus our account-identity guard, while `Environment.SANDBOX` simulates fills locally and is a different thing entirely (`SIMULATED` vs `PAPER_VERIFIED`). Conflating the two would be exactly the kind of false labelling AGENTS.md invariant 11 forbids, so mode translation is explicit in code and tested in CR-04.

## Decision

1. **External package, no upstream fork.** The adapter lives in `src/tigerquant/adapters/nautilus/` and depends on the published wheel. We do not patch NautilusTrader. Upgrades are wheel bumps guarded by the compatibility test.
2. **Two execution clients, one contract.** `TigerPaperExecutionClient` (real Tiger paper, CR-03/CR-04) and `SimulatedExecutionClient` (offline, CR-01/CR-04) implement the same internal `BrokerProtocol` *and* the NautilusTrader execution-client interface. Parity tests drive both through the identical scenario fixture and assert identical internal events; only the transport differs.
3. **The engine is not the risk authority and not the audit store.** An order leaving the strategy layer is already an `ApprovedOrder` (ADR-0007) and is journaled (ADR-0004) before the engine sees it. The engine may reject it further; it may never approve something the gateway denied. Engine state is treated as a cache that is rebuilt from the journal.
4. **Two-wire design for the paper path.** NautilusTrader is used for its event-driven lifecycle, order state management and backtest/paper parity. The *only* place a broker write originates is the Tiger adapter's `submit_order`, which accepts `ApprovedOrder` only.
5. **Session and normalisation.** US equity sessions, halts and half-days are resolved through an explicit exchange-calendar component (CR-05) rather than engine defaults; timestamps are normalised to UTC at the adapter boundary and the venue timezone is carried on the instrument. This is required by acceptance gate 14.
6. **Backtest/paper parity is tested, not assumed.** CR-04 introduces a shared scenario corpus; the deterministic simulator and the paper adapter must produce the same *internal* order/fill event sequence for the same inputs. Real-world divergence (queue position, partial fills, rejections) is recorded as a labelled difference, never smoothed over.
7. **Documented caveat.** Broker paper executions do not establish real liquidity, slippage or production fill certainty and are reported as `PAPER_VERIFIED`, never `LIVE_VERIFIED`.

## Consequences

- CR-01 must not depend on NautilusTrader: the offline slice defines the *internal* event and strategy contracts that the adapter will later satisfy. This keeps the fast CI lane free of a large compiled wheel while preserving the intended architecture.
- If `nautilus_trader` cannot be installed in a future environment, CR-04 is blocked but CR-01/02/03/05/06/07 remain testable; the internal contracts are deliberately engine-agnostic.
