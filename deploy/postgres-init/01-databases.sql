-- TigerQuant — per-mode database and role isolation.
--
-- Runs once, on an empty data volume, via /docker-entrypoint-initdb.d.
--
-- Why separate databases rather than separate schemas: a schema shares one role namespace
-- and one set of default privileges, so a single mistaken GRANT opens every mode at once.
-- Separate databases mean a process running in PAPER holds a credential that physically
-- cannot open the SHADOW or LIVE database (AGENTS.md invariant 2, ADR-0003 §5).
--
-- Implementation note: `CREATE DATABASE` cannot run inside a transaction or a PL/pgSQL
-- function, so a `DO $$ ... $$` loop fails with "CREATE DATABASE cannot be executed from a
-- function" and rolls the whole block back. `\gexec` executes each row of a query as a
-- standalone statement, which is the supported way to do conditional DDL here.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. Tighten the template first, so databases created below inherit an ACL with no
--    implicit PUBLIC CONNECT. Doing this after creation would leave a window in which a
--    new database was world-connectable.
-- ---------------------------------------------------------------------------
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- 2. One login role per mode. NOSUPERUSER / NOCREATEDB / NOCREATEROLE so a compromised
--    mode credential cannot escalate into the others or into the cluster.
--
--    Passwords are development-only and match deploy/compose.yaml's rationale: they open a
--    loopback-only database, a fresh clone must work with one command, and no real
--    deployment ever uses them. A real deployment injects credentials from a secret
--    manager and never writes them to a file in the repository.
-- ---------------------------------------------------------------------------
SELECT format(
           'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
           mode_role,
           'tq_local_development_only'
       )
FROM (VALUES ('tq_backtest'), ('tq_shadow'), ('tq_paper'), ('tq_live')) AS modes (mode_role)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = modes.mode_role)
\gexec

-- ---------------------------------------------------------------------------
-- 3. One database per mode, owned by its own role.
-- ---------------------------------------------------------------------------
SELECT format('CREATE DATABASE %I OWNER %I', mode_role, mode_role)
FROM (VALUES ('tq_backtest'), ('tq_shadow'), ('tq_paper'), ('tq_live')) AS modes (mode_role)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = modes.mode_role)
\gexec

-- ---------------------------------------------------------------------------
-- 4. Make the isolation explicit rather than inherited: revoke everything from PUBLIC and
--    grant CONNECT only to the owning mode role. The bootstrap role keeps CONNECT for
--    administrative tooling and is never the role the application uses.
-- ---------------------------------------------------------------------------
REVOKE ALL ON DATABASE tq_backtest FROM PUBLIC;
REVOKE ALL ON DATABASE tq_shadow   FROM PUBLIC;
REVOKE ALL ON DATABASE tq_paper    FROM PUBLIC;
REVOKE ALL ON DATABASE tq_live     FROM PUBLIC;

GRANT CONNECT ON DATABASE tq_backtest TO tq_backtest;
GRANT CONNECT ON DATABASE tq_shadow   TO tq_shadow;
GRANT CONNECT ON DATABASE tq_paper    TO tq_paper;
GRANT CONNECT ON DATABASE tq_live     TO tq_live;

GRANT CONNECT ON DATABASE tq_backtest TO tq_bootstrap;
GRANT CONNECT ON DATABASE tq_shadow   TO tq_bootstrap;
GRANT CONNECT ON DATABASE tq_paper    TO tq_bootstrap;
GRANT CONNECT ON DATABASE tq_live     TO tq_bootstrap;

-- ---------------------------------------------------------------------------
-- 5. Per-database baseline: the mode role owns the schema it will migrate, and PUBLIC
--    keeps no rights on it. Migrations then run as the mode role, so one mode's migration
--    can never touch another mode's objects.
-- ---------------------------------------------------------------------------
\connect tq_backtest
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO tq_backtest;
GRANT USAGE, CREATE ON SCHEMA public TO tq_backtest;

\connect tq_shadow
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO tq_shadow;
GRANT USAGE, CREATE ON SCHEMA public TO tq_shadow;

\connect tq_paper
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO tq_paper;
GRANT USAGE, CREATE ON SCHEMA public TO tq_paper;

\connect tq_live
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO tq_live;
GRANT USAGE, CREATE ON SCHEMA public TO tq_live;
