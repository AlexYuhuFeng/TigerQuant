# Architecture Decision Records — TigerQuant

Each ADR records one decision that is expensive to reverse. Status values: `Accepted`, `Superseded by ADR-NNNN`, `Proposed`.

| ADR | Title | Status |
|---|---|---|
| [0001](ADR-0001-runtime-and-dependency-pinning.md) | Runtime, layering and dependency pinning | Accepted |
| [0002](ADR-0002-module-layout-and-process-topology.md) | Module layout and process topology | Accepted |
| [0003](ADR-0003-mode-and-account-isolation.md) | Mode and account isolation | Accepted |
| [0004](ADR-0004-event-sourcing-and-audit-journal.md) | Event sourcing and append-only audit journal | Accepted |
| [0005](ADR-0005-evidence-store-and-point-in-time-snapshots.md) | Evidence store and point-in-time snapshots | Accepted |
| [0006](ADR-0006-decision-contract-and-deterministic-recomputation.md) | Decision contract and deterministic recomputation | Accepted |
| [0007](ADR-0007-risk-gateway-authority.md) | Risk gateway is an independent authority | Accepted |
| [0008](ADR-0008-order-intent-state-machine.md) | Order intent identity and state machine | Accepted |
| [0009](ADR-0009-model-provider-neutrality.md) | Model provider neutrality | Accepted |
| [0010](ADR-0010-nautilustrader-adapter.md) | NautilusTrader integration as an external adapter | Accepted |
| [0011](ADR-0011-deterministic-time-and-identifiers.md) | Deterministic time and identifiers | Accepted |
| [0012](ADR-0012-tiger-oauth-read-only-scope.md) | Tiger OAuth 2.0 with a read-only scope ceiling | Accepted |
| [0013](ADR-0013-database-architecture.md) | Database architecture: portable PostgreSQL projections beside a portable journal | Accepted |

Companion documents: [THREAT_MODEL.md](../THREAT_MODEL.md), [COMPONENT_MAP.md](../COMPONENT_MAP.md), [DEPENDENCIES.md](../DEPENDENCIES.md), [CR-01-VERTICAL-SLICE.md](../CR-01-VERTICAL-SLICE.md).
