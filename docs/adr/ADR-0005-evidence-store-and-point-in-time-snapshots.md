# ADR-0005 — Evidence store and point-in-time snapshots

- Status: Accepted
- Date: 2026-10-10
- CR: CR-00 (implemented in CR-05; contract types appear in CR-01)

## Context

Invariant 4 ("no evidence means NO_TRADE") and invariant 6 (provenance, as-of correctness, lookahead prevention) are the core of the product's claim to be *evidence-grounded*. ARCHITECTURE.md specifies typed facts, a claim-to-evidence graph, and a frozen decision-time snapshot with a cryptographic digest. Acceptance gate 12 requires that historical replay cannot see future revisions.

## Decision

1. **Typed facts.** `Fact` records carry `kind ∈ {OBSERVED, DERIVED, ATTRIBUTED_CLAIM, ESTIMATE, PREDICTIVE_BELIEF}`. `verified` attests record validity/provenance/consistency only — never that a market theory is true. This wording is enforced in the docstrings and the dashboard legend.
2. **Five timestamps minimum**: `event_time` (when it happened in the world), `published_at` (when the source published), `available_at` (earliest instant the system could legitimately have known it), `ingested_at` (when we actually fetched it), `valid_until` (staleness horizon). Every derived feature inherits the *maximum* of its inputs' `available_at`, which is the formal lookahead guard.
3. **Point-in-time store, not a mutable table.** Facts are immutable and content-addressed (`sha256` of canonical JSON). Revisions are new facts with `supersedes` links. An `EvidenceStore.as_of(t)` query returns only facts with `available_at <= t` and the revision effective at `t`. No API exists to read "the latest" without an explicit `as_of`.
4. **Snapshot freezing.** `EvidenceSnapshot` is an immutable, ordered list of resolved fact IDs plus a digest, the `as_of` time, the requester, the instrument allowlist version and a completeness verdict. The model receives the snapshot digest, and its output must reference IDs that resolve *inside that snapshot*. Unresolvable IDs → rejection (gate 9).
5. **Staleness and contradiction are first-class denials.** A snapshot has `max_age` per fact category. Any key input that is missing, stale beyond `valid_until`, or contradicted by another in-window fact yields `SnapshotStatus = INCOMPLETE | CONTRADICTORY`, which forces `NO_TRADE` or a risk freeze — never a manufactured consensus.
6. **Untrusted content is sandboxed.** Retrieved text is data, never instructions. It is stored with provenance and a content digest, is never concatenated into an instruction position, and is wrapped in explicit untrusted-content delimiters in prompts. Injection patterns are detected and recorded as a quality flag; a detected injection cannot raise the snapshot's completeness.
7. **Licensing.** Each fact carries `license_scope` so that redistribution-restricted content can be excluded from exports and the dashboard.

## Consequences

- The evidence store is deliberately simple in CR-01 (in-memory + JSONL) and grows to a real store in CR-05; the interface (`as_of`, no mutable reads) is fixed now so CR-05 is an implementation swap, not a redesign.
- Because revisions are appended rather than updated, storage grows; CR-05 must define retention for `valid_until`-expired facts without breaking reproducibility of a recorded decision (the decision's snapshot digest pins exactly what was seen).
