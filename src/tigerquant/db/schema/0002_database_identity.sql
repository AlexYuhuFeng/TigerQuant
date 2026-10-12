-- Record which MODE this database is for, so a connection to the wrong one is detectable.
--
-- ADR-0013 §2 says the database boundary IS the isolation mechanism, and that storing the mode as
-- a column would invite a `WHERE mode = ...` filter a future query could forget. This table does
-- not contradict that, and the distinction matters:
--
--   * the mode is still NOT on any row. No query filters by it, and none should.
--   * this is a single row describing the DATABASE ITSELF, so a process can assert that the
--     database it connected to is the one it meant to. That is a guard against misconfiguration,
--     not a substitute for the boundary.
--
-- The row is deliberately NOT created here: SQL cannot know which mode a database is for. The
-- runner stamps it once via `db.identity.declare_mode`, and an EMPTY table therefore means
-- "undeclared", which is refused rather than assumed to match.

CREATE TABLE IF NOT EXISTS database_identity (
    -- At most one row. The check constraint makes that structural rather than conventional.
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    mode        text NOT NULL,
    declared_at timestamptz NOT NULL DEFAULT now(),
    declared_by text NOT NULL DEFAULT ''
);

COMMENT ON TABLE database_identity IS
    'Which Mode this database serves. One row. Absence means undeclared, which callers must refuse.';
