"""Projection storage: PostgreSQL in deployment, in-memory offline.

Layering (ADR-0002): the domain depends on the :class:`~tigerquant.db.repository.Repository`
protocol, never on this package's driver. ``psycopg`` is imported lazily and only inside
:mod:`tigerquant.db.postgres`, so importing :mod:`tigerquant.db` requires nothing extra.
"""

from __future__ import annotations

from tigerquant.db.lease import (
    DEFAULT_LEASE_SECONDS,
    InMemoryLease,
    Lease,
    LeaseError,
    LeaseManager,
    LeaseState,
)
from tigerquant.db.migrations import (
    MIGRATIONS_DIR,
    AppliedMigration,
    Migration,
    MigrationError,
    MigrationPlan,
    MigrationRunner,
    content_digest,
    discover_migrations,
)
from tigerquant.db.repository import (
    AlertRow,
    DecisionRow,
    InMemoryRepository,
    PortfolioStateRow,
    Repository,
    RepositoryHealth,
    RiskOutcomeRow,
)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "MIGRATIONS_DIR",
    "AlertRow",
    "AppliedMigration",
    "DecisionRow",
    "InMemoryLease",
    "InMemoryRepository",
    "Lease",
    "LeaseError",
    "LeaseManager",
    "LeaseState",
    "Migration",
    "MigrationError",
    "MigrationPlan",
    "MigrationRunner",
    "PortfolioStateRow",
    "Repository",
    "RepositoryHealth",
    "RiskOutcomeRow",
    "content_digest",
    "discover_migrations",
]
