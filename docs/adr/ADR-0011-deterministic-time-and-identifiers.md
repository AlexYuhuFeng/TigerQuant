# ADR-0011 — Deterministic time and identifiers

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (implemented in CR-01)

## Context

Acceptance gate 12 (point-in-time replay, no future leakage) and the CR-01 gate (end-to-end synthetic replay) both require reproducibility. The strongest available test is: *the same fixture produces a byte-identical audit journal on every run and on every machine*. Wall-clock reads and random UUIDs make that impossible, and also make it impossible to tell whether a historical replay accidentally saw future data.

## Decision

1. **Time is injected.** All production code takes a `Clock` (`now() -> datetime`, always timezone-aware UTC). Two implementations: `SystemClock` (real time) and `SimulatedClock` (advances only when the pipeline tells it to). Direct `datetime.now()`, `time.time()` and `pandas.Timestamp.now()` are banned in `src/tigerquant` by a lint rule test that greps the source tree; only `core/clock.py` may call them.
2. **Identifiers are deterministic.** `IdFactory` mints IDs from `(namespace, monotonic counter, clock timestamp)` using a Crockford-base32 encoding of a 48-bit millisecond timestamp plus a per-process counter plus a namespace hash. There is no `uuid4()` anywhere in `src/tigerquant`. In replay, the counter starts at zero, so IDs are stable across runs.
3. **Ordering is total.** Events carry a strictly increasing `seq` from a single `EventSequencer` per journal. Any collection that influences output is an ordered structure (`list`, `tuple`, insertion-ordered `dict`); iteration over a `set` in an output path is forbidden and covered by a determinism test that re-runs the slice in a fresh interpreter.
4. **Canonical serialisation.** One function (`core.canonical.canonical_json_bytes`) defines hashing and journal encoding: sorted keys, compact separators, UTF-8, `Decimal` → normalised string (`quantize` to the field's declared scale), `datetime` → UTC ISO-8601 microseconds, `Enum` → value. Hashing anything else is a bug.
5. **Money is `Decimal`, never `float`.** Prices quantise to 4 dp, cash/notional to 2 dp, weights to 8 dp, quantities are integral shares. Rounding is `ROUND_HALF_EVEN` with an explicit, documented policy per field, so recomputation is bit-stable (ADR-0006).
6. **Clock skew is detectable.** Every external event carries both our `ingested_at` and the source's `available_at`; a negative or implausible delta raises an alert (gate 15) rather than being silently clamped.

## Consequences

- Reproducibility is testable end-to-end from CR-01 onward, giving every later CR a cheap regression detector.
- A small amount of ceremony (passing clocks and id factories) is paid everywhere in exchange for being able to prove the absence of lookahead and to reconstruct any historical decision exactly.
- Real-time behaviour (latency, staleness) is measured against `SystemClock` in paper mode, where reproducibility is not required but provenance still is.
