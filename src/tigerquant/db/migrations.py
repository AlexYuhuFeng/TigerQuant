"""Forward-only, checksummed SQL migrations.

Design (ADR-0013 §3):

* migrations are numbered SQL files discovered from ``db/schema``;
* applied versions are recorded with the **checksum of the file that was applied**;
* re-running is a no-op;
* if an already-applied migration's file content changed, the runner **refuses to start**
  rather than applying a divergent schema.

That last property is the same tamper-evidence principle the audit journal uses, applied to
DDL: a schema change must be a new, reviewed migration, never an edit to history.

The runner is deliberately tiny and dependency-free. It talks to a *connection object*
satisfying a three-method protocol, so it is fully testable offline with a fake connection —
which is how the ordering, checksum and idempotency behaviour is verified in this
environment, where no PostgreSQL server is available.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from tigerquant.core.canonical import sha256_hex
from tigerquant.core.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "MIGRATIONS_DIR",
    "AppliedMigration",
    "Migration",
    "MigrationError",
    "MigrationPlan",
    "MigrationRunner",
    "discover_migrations",
]

#: Where the numbered ``.sql`` files live.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "schema"

#: ``0001_initial.sql`` -> version 1, name ``initial``.
_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


class MigrationError(ConfigurationError):
    """Migrations cannot be applied safely."""


class Connection(Protocol):
    """The slice of a DB-API connection the runner needs.

    Kept to three methods so the runner can be driven by a fake in tests and by any
    DB-API 2.0 driver in deployment, without importing one here.
    """

    def execute(self, sql: str, params: Sequence[object] = ...) -> object: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered SQL file."""

    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """A row of ``schema_migrations``."""

    version: int
    name: str
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What :meth:`MigrationRunner.plan` decided to do."""

    pending: tuple[Migration, ...]
    already_applied: tuple[AppliedMigration, ...]

    @property
    def is_noop(self) -> bool:
        return not self.pending

    def describe(self) -> str:
        if self.is_noop:
            return f"up to date ({len(self.already_applied)} migration(s) applied)"
        labels = ", ".join(migration.label for migration in self.pending)
        return f"pending: {labels}"


def _checksum(sql: str) -> str:
    """Checksum of a migration's normalised text.

    Line endings are normalised first: the same file checked out on Windows and Linux must
    produce the same checksum, or every cross-platform run would look like tampering.
    """
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    return f"sha256:{sha256_hex(normalised.encode('utf-8'))}"


def discover_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    """Find, parse and order every migration file.

    Raises
    ------
    MigrationError
        If a filename does not match the convention, or two files claim the same version.
    """
    base = directory or MIGRATIONS_DIR
    if not base.is_dir():
        msg = f"migrations directory does not exist: {base}"
        raise MigrationError(msg)

    found: dict[int, Migration] = {}
    for path in sorted(base.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            msg = f"migration filename {path.name!r} does not match 'NNNN_lower_snake_name.sql'"
            raise MigrationError(msg)
        version = int(match.group("version"))
        name = match.group("name")
        if version in found:
            msg = (
                f"duplicate migration version {version}: {found[version].path.name} and {path.name}"
            )
            raise MigrationError(msg)
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            msg = f"migration {path.name} is empty"
            raise MigrationError(msg)
        found[version] = Migration(
            version=version, name=name, path=path, sql=sql, checksum=_checksum(sql)
        )

    if not found:
        msg = f"no migrations found in {base}"
        raise MigrationError(msg)
    return tuple(found[key] for key in sorted(found))


class MigrationRunner:
    """Applies migrations to one database.

    One runner per database: every mode has its own database and is migrated by the same
    code path with its own connection (ADR-0013 §2).
    """

    __slots__ = ("_migrations",)

    def __init__(self, migrations: Iterable[Migration] | None = None) -> None:
        self._migrations = tuple(migrations) if migrations is not None else discover_migrations()

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return self._migrations

    @property
    def head_version(self) -> int:
        return self._migrations[-1].version if self._migrations else 0

    # -- inspection --------------------------------------------------------------

    def ensure_ledger(self, connection: Connection) -> None:
        """Create ``schema_migrations`` if it does not exist.

        Defined here rather than in migration 0001 so that the ledger itself is never
        subject to a migration: a broken 0001 must still be diagnosable.
        """
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    integer     PRIMARY KEY,
                name       text        NOT NULL,
                checksum   text        NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        connection.commit()

    def applied(self, connection: Connection) -> tuple[AppliedMigration, ...]:
        """Rows of ``schema_migrations``, oldest first."""
        cursor = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        fetch_all = getattr(cursor, "fetchall", None)
        rows = fetch_all() if callable(fetch_all) else ()
        return tuple(
            AppliedMigration(version=int(row[0]), name=str(row[1]), checksum=str(row[2]))
            for row in rows
        )

    def plan(self, connection: Connection) -> MigrationPlan:
        """Decide what to apply, and refuse if history has been rewritten."""
        self.ensure_ledger(connection)
        applied = self.applied(connection)
        self._assert_history_intact(applied)

        applied_versions = {row.version for row in applied}
        pending = tuple(m for m in self._migrations if m.version not in applied_versions)
        return MigrationPlan(pending=pending, already_applied=applied)

    def _assert_history_intact(self, applied: Sequence[AppliedMigration]) -> None:
        """Refuse to run when an applied migration's file has changed."""
        by_version = {migration.version: migration for migration in self._migrations}
        for row in applied:
            known = by_version.get(row.version)
            if known is None:
                msg = (
                    f"database has migration {row.version:04d}_{row.name} applied, but this "
                    "build has no such migration; refusing to run against a newer schema"
                )
                raise MigrationError(msg)
            if known.checksum != row.checksum:
                msg = (
                    f"migration {known.label} was applied with checksum {row.checksum} but the "
                    f"file now hashes to {known.checksum}. Applied migrations are immutable; "
                    "add a new migration instead of editing history."
                )
                raise MigrationError(msg)

    # -- application -------------------------------------------------------------

    def apply(self, connection: Connection, *, dry_run: bool = False) -> tuple[Migration, ...]:
        """Apply every pending migration in order, inside one transaction per migration.

        Returns the migrations that were applied. ``dry_run`` reports the plan without
        touching the database.
        """
        plan = self.plan(connection)
        if dry_run:
            return plan.pending

        done: list[Migration] = []
        for migration in plan.pending:
            try:
                connection.execute(migration.sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            except Exception as exc:
                connection.rollback()
                msg = f"migration {migration.label} failed and was rolled back: {exc}"
                raise MigrationError(msg) from exc
            connection.commit()
            done.append(migration)
        return tuple(done)

    # -- reporting ---------------------------------------------------------------

    def status(self, connection: Connection) -> str:
        """One-line human-readable state, for ``doctor`` and operator tooling."""
        applied = {row.version for row in self.applied(connection)}
        lines = []
        for migration in self._migrations:
            state = "applied" if migration.version in applied else "PENDING"
            lines.append(f"  {migration.label:<28} {state}")
        return "\n".join(lines)


def content_digest(migrations: Sequence[Migration]) -> str:
    """Digest over every migration's checksum, for the release evidence record."""
    material = "|".join(f"{m.version}:{m.checksum}" for m in migrations)
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
