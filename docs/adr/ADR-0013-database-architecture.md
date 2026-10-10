# ADR-0013 — Database architecture: portable PostgreSQL projections beside a portable journal

- Status: Accepted
- Date: 2026-10-10
- CR: CR-05 (evidence durability), CR-08 (dashboard), CR-09 (long-duration runs)
- Extends: ADR-0004 (journal), ADR-0005 (evidence store), ADR-0008 (order state machine)

## Context

TigerQuant has several kinds of durable state with different requirements, and the
constitution constrains how they may be isolated:

| State | Written by | Read by | Requirement |
|---|---|---|---|
| Audit journal | every component | humans, verification, replay | append-only, tamper-evident, portable, survives vendor loss |
| Evidence facts | ingestion | decision path, replay | point-in-time queries, revisions, licence scope |
| Order/fill/position projections | execution | risk, dashboard, reconciliation | concurrent reads, rebuildable |
| Worker lease | one process per mode | every candidate process | mutual exclusion across processes and machines |
| Decisions and risk outcomes | decision/risk planes | dashboard, review | queryable lineage |

AGENTS.md invariant 2 requires mode isolation to be enforced by "isolated account
identities, secrets, workers, order namespaces, **databases or strongly enforced data
partitions**, and audit trails". Acceptance gate 15 requires that a **database outage**
produces an alert and no new exposure. ADR-0004 already establishes that the journal — not
any database — is the primary record.

The development container image must also be the same one used in CI and on any operator
workstation, across Windows, macOS and Linux, because a database that only runs on one of
them is not a migration strategy.

## Decision

### 1. Two tiers, with a defined precedence

- **Tier 1 — the append-only hash-chained journal (JSONL).** Authoritative, portable and
  independently verifiable with nothing but a text reader and `sha256`. It is written and
  flushed **before** any projection is updated, and it is the artifact a human reviews.
- **Tier 2 — PostgreSQL.** Queryable projections, the evidence index, and cross-process
  coordination (the worker lease). **Rebuildable at any time** from Tier 1 by replaying it.

Consequence: losing the database loses no history, and a database that disagrees with the
journal is a bug that is detectable rather than a silent fork.

### 2. PostgreSQL, containerised and version-pinned

PostgreSQL is chosen over SQLite or an embedded store for four reasons that map directly to
acceptance gates: **transactional DDL** (migrations that cannot half-apply), **advisory
locks** (the cross-process worker lease of gate 8), **database-per-mode isolation** with
separate roles (invariant 2), and **concurrent readers** for the CR-08 dashboard without
blocking the execution writer.

Deployment is container-first:

- `deploy/compose.yaml` defines the database with a **pinned image digest**, a
  `pg_isready` healthcheck, and a named volume. `docker compose up -d` is the only setup
  step on any OS.
- `deploy/Dockerfile` builds the application image from the same `pyproject.toml` and
  `uv.lock` used everywhere else, so the runtime is identical across platforms.
- `deploy/postgres-init/` runs once on an empty volume and creates one **database and one
  role per mode** — `tq_backtest`, `tq_shadow`, `tq_paper`, `tq_live` — with no grants
  between them. A process running in one mode has no credential that can reach another
  mode's data.

### 3. Migrations are forward-only, numbered and checksummed

`db/migrations.py` applies numbered SQL files in order and records
`(version, name, checksum, applied_at)` in `schema_migrations`. Re-running is a no-op. If
an already-applied migration's checksum changed, the runner **refuses to start** rather
than applying a divergent schema — the same tamper-evidence principle as the journal,
applied to DDL.

No ORM. The schema is explicit SQL, which keeps the migration diff reviewable by a human
and removes a large dependency from the trust boundary.

### 4. One repository protocol, two implementations, honest labelling

`db/repository.py` defines what the domain needs (append event, upsert projections, query
as-of, acquire lease). Two implementations satisfy it:

- **`InMemoryRepository`** — used by the offline fast lane and unit tests. It is the
  *contract test target*: the same behavioural suite runs against both.
- **`PostgresRepository`** — the deployment implementation, importing `psycopg` lazily and
  raising `IntegrationUnavailable` when the `db` extra is absent (ADR-0001 §6).

Tests that need a real server are marked `requires_db` and are **never** reported as
passing when they have not run. A green fast lane says nothing about the SQL.

### 5. Outage behaviour: refuse new exposure, keep recording

When the database is unreachable the worker must:

1. continue appending to Tier 1 (the journal is a local file and does not depend on the
   database);
2. **refuse new exposure** — projections are stale, so the startup gate stays closed and
   the risk state is frozen (`ReasonCode.UNRECONCILED`);
3. raise a CRITICAL alert and keep reconciliation running.

This is gate 15, and it is the reason the risk gateway already has a
`no_blocking_intents` check and a `STARTUP_GATE_CLOSED` denial: no new code is needed in
the domain, only correct wiring.

### 6. What must never be in the database

- No credentials, private keys or tokens (AGENTS.md "Security").
- No full account numbers: the guard stores the **masked** form plus reported attributes.
- No licensed content beyond its `license_scope`; `RESTRICTED` facts are not projected at all.
- No model access. The decision plane never opens a database connection; it receives a
  frozen snapshot. A model that could query the database would be an unbounded evidence
  channel, which is exactly what ADR-0005 exists to prevent.

## Consequences

- Two stores must be kept consistent, but the direction is fixed: journal → database. A
  check that recomputes projections from the journal and compares is straightforward and is
  the CR-09 drift detector.
- Containerisation adds a Docker dependency for full-stack runs. The **fast lane does not
  need it**: it uses `InMemoryRepository`, so unit and contract tests still run anywhere,
  which preserves the CR-01 promise that the offline slice needs no infrastructure.
- Mode isolation is now enforced at the database-credential level, not only in Python. A
  bug that tries to read another mode's data fails on permissions, which is a stronger
  guarantee than a namespace prefix.
- Schema changes are a reviewed, versioned repository event with a recorded checksum.
- **Not verifiable in this development environment:** Docker Desktop is not running and the
  daemon is reachable only through a Windows named pipe, which the DSH sandbox blocks.
  Everything that does not require the daemon is implemented and tested offline; the
  Postgres-specific tests are marked `requires_db` and are reported as **not executed**,
  never as passing.
