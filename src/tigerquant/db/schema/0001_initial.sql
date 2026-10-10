-- TigerQuant migration 0001 — initial projection schema.
--
-- Design notes (ADR-0013):
--
-- * Every table is keyed by nothing mode-specific: the mode is implied by the DATABASE the
--   connection is in. There is deliberately no `mode` column to filter on, because a
--   missing WHERE clause would then silently mix modes. The database boundary is the
--   isolation mechanism.
-- * The journal remains authoritative. Everything here is a rebuilding projection, which
--   is why `events` stores the full canonical payload: a projection can always be
--   recomputed from it, and the row's own hash chain lets the database be checked against
--   the JSONL journal.
-- * Money is `numeric`, never floating point (ADR-0011 §5). Weights use numeric(20,8).
-- * Timestamps are `timestamptz` and always UTC; a naive timestamp is a bug everywhere.

-- ---------------------------------------------------------------------------
-- Applied migrations, with checksums. The runner refuses to start if an applied
-- migration's checksum has changed (ADR-0013 §3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer     PRIMARY KEY,
    name        text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- The event log mirror. Append-only in practice: the runner grants INSERT/SELECT only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    seq             bigint      NOT NULL,
    event_id        text        NOT NULL,
    event_type      text        NOT NULL,
    schema_version  text        NOT NULL,
    occurred_at     timestamptz NOT NULL,
    recorded_at     timestamptz NOT NULL,
    correlation_id  text        NOT NULL,
    causation_id    text,
    payload         jsonb       NOT NULL,
    prev_hash       text        NOT NULL,
    hash            text        NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (seq),
    UNIQUE (event_id),
    CONSTRAINT events_hash_shape CHECK (hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT events_prev_shape CHECK (prev_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS events_type_time_idx ON events (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS events_correlation_idx ON events (correlation_id);

-- ---------------------------------------------------------------------------
-- Order intents: the folded state of the state machine (ADR-0008).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    intent_id           text        PRIMARY KEY,
    decision_id         text        NOT NULL,
    instrument          text        NOT NULL,
    side                text        NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity            integer     NOT NULL CHECK (quantity > 0),
    filled_quantity     integer     NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    remaining_quantity  integer     NOT NULL CHECK (remaining_quantity >= 0),
    order_type          text        NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT')),
    time_in_force       text        NOT NULL CHECK (time_in_force IN ('DAY', 'GTC')),
    limit_price         numeric(18, 4) CHECK (limit_price IS NULL OR limit_price > 0),
    client_order_id     text        NOT NULL UNIQUE,
    status              text        NOT NULL,
    broker_order_id     text,
    average_fill_price  numeric(18, 4) CHECK (average_fill_price IS NULL OR average_fill_price > 0),
    commission          numeric(18, 4) NOT NULL DEFAULT 0 CHECK (commission >= 0),
    attempts            integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    reason_code         text,
    updated_seq         bigint      NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT orders_filled_within_quantity CHECK (filled_quantity <= quantity)
);

CREATE INDEX IF NOT EXISTS orders_open_idx ON orders (status) WHERE status NOT IN ('FILLED', 'CANCELED', 'REJECTED');

-- ---------------------------------------------------------------------------
-- Fills. `exec_id` is the idempotency key: re-delivering a broker fill cannot book twice
-- (acceptance gate 7).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    exec_id      text        PRIMARY KEY,
    intent_id    text        NOT NULL REFERENCES orders (intent_id) ON DELETE RESTRICT,
    instrument   text        NOT NULL,
    side         text        NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity     integer     NOT NULL CHECK (quantity > 0),
    price        numeric(18, 4) NOT NULL CHECK (price > 0),
    commission   numeric(18, 4) NOT NULL DEFAULT 0 CHECK (commission >= 0),
    occurred_at  timestamptz NOT NULL,
    seq          bigint      NOT NULL
);

CREATE INDEX IF NOT EXISTS fills_intent_idx ON fills (intent_id);

-- ---------------------------------------------------------------------------
-- Positions and portfolio state: projections of the fills above.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    instrument    text        PRIMARY KEY,
    quantity      integer     NOT NULL,
    average_cost  numeric(18, 4) NOT NULL CHECK (average_cost >= 0),
    realized_pnl  numeric(18, 4) NOT NULL DEFAULT 0,
    updated_seq   bigint      NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    -- Long-only by construction: the database refuses a short even if application code
    -- is wrong (AGENTS.md invariant 9).
    CONSTRAINT positions_long_only CHECK (quantity >= 0)
);

CREATE TABLE IF NOT EXISTS portfolio_state (
    singleton       boolean     PRIMARY KEY DEFAULT true CHECK (singleton),
    cash            numeric(18, 2) NOT NULL,
    equity          numeric(18, 2) NOT NULL,
    gross_exposure  numeric(18, 2) NOT NULL DEFAULT 0 CHECK (gross_exposure >= 0),
    net_exposure    numeric(18, 2) NOT NULL DEFAULT 0,
    ledger_digest   text        NOT NULL,
    updated_seq     bigint      NOT NULL,
    as_of           timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- Evidence facts: the queryable index over the point-in-time store (ADR-0005).
-- `available_at` is the column every read filters on; there is no "latest" read path.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_facts (
    fact_id         text        PRIMARY KEY,
    kind            text        NOT NULL,
    category        text        NOT NULL,
    instrument      text,
    event_time      timestamptz NOT NULL,
    published_at    timestamptz NOT NULL,
    available_at    timestamptz NOT NULL,
    ingested_at     timestamptz NOT NULL,
    valid_until     timestamptz NOT NULL,
    source_id       text        NOT NULL,
    content_hash    text        NOT NULL,
    license_scope   text        NOT NULL DEFAULT 'INTERNAL',
    quality_flags   text[]      NOT NULL DEFAULT '{}',
    supersedes      text,
    attributes      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT facts_time_order CHECK (available_at >= event_time AND ingested_at >= available_at)
);

CREATE INDEX IF NOT EXISTS facts_asof_idx ON evidence_facts (available_at, category);
CREATE INDEX IF NOT EXISTS facts_instrument_idx ON evidence_facts (instrument, category, event_time);
CREATE INDEX IF NOT EXISTS facts_supersedes_idx ON evidence_facts (supersedes) WHERE supersedes IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Decisions and risk outcomes: the reviewable lineage (ADR-0006, ADR-0007).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    decision_id      text        PRIMARY KEY,
    snapshot_id      text        NOT NULL,
    model_version    text        NOT NULL,
    prompt_version   text        NOT NULL,
    action           text        NOT NULL CHECK (action IN ('NO_TRADE', 'HOLD', 'SET_TARGET', 'REDUCE', 'CLOSE')),
    instrument       text,
    target_weight    numeric(20, 8) CHECK (target_weight IS NULL OR (target_weight >= 0 AND target_weight <= 1)),
    horizon          text        NOT NULL,
    thesis           text        NOT NULL,
    counter_thesis   text        NOT NULL,
    invalidation     text        NOT NULL,
    evidence_ids     text[]      NOT NULL DEFAULT '{}',
    uncertainties    text[]      NOT NULL DEFAULT '{}',
    seq              bigint      NOT NULL,
    occurred_at      timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS decisions_action_idx ON decisions (action, occurred_at);

CREATE TABLE IF NOT EXISTS risk_outcomes (
    seq              bigint      PRIMARY KEY,
    intent_id        text        NOT NULL,
    outcome          text        NOT NULL CHECK (outcome IN ('ALLOW', 'DENY')),
    reason_codes     text[]      NOT NULL DEFAULT '{}',
    policy_hash      text        NOT NULL,
    portfolio_digest text        NOT NULL,
    checks_run       text[]      NOT NULL DEFAULT '{}',
    detail           text        NOT NULL DEFAULT '',
    occurred_at      timestamptz NOT NULL,
    -- A DENY without a reason is a bug, not a data-quality issue.
    CONSTRAINT risk_deny_has_reason CHECK (outcome = 'ALLOW' OR cardinality(reason_codes) > 0)
);

CREATE INDEX IF NOT EXISTS risk_intent_idx ON risk_outcomes (intent_id);

-- ---------------------------------------------------------------------------
-- Alerts, for the operator surface and the CR-09 false-alert measurement.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    seq          bigint      PRIMARY KEY,
    severity     text        NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'CRITICAL')),
    code         text        NOT NULL,
    message      text        NOT NULL,
    occurrences  integer     NOT NULL DEFAULT 1 CHECK (occurrences > 0),
    context      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS alerts_severity_idx ON alerts (severity, occurred_at);

-- ---------------------------------------------------------------------------
-- Worker lease (ADR-0008 §7, acceptance gate 8). Exactly one execution owner per database.
-- A row-based lease is used rather than a session advisory lock so that the lease survives
-- a reconnect and is visible to an operator; the acquisition itself is still atomic.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_leases (
    lease_name    text        PRIMARY KEY,
    owner         text        NOT NULL,
    acquired_at   timestamptz NOT NULL,
    heartbeat_at  timestamptz NOT NULL,
    expires_at    timestamptz NOT NULL,
    CONSTRAINT lease_expiry_after_heartbeat CHECK (expires_at > heartbeat_at)
);

-- ---------------------------------------------------------------------------
-- Append-only enforcement for the tables that model history.
--
-- The projections (`orders`, `positions`, `portfolio_state`) are updated in place because
-- they are derived state. `events` and `fills` are history and must never be rewritten: a
-- trigger raises if an UPDATE or DELETE is attempted, so a bug in application code cannot
-- quietly rewrite what happened.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION tq_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only; % is not permitted (ADR-0004)',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION tq_reject_mutation();

DROP TRIGGER IF EXISTS fills_append_only ON fills;
CREATE TRIGGER fills_append_only
    BEFORE UPDATE OR DELETE ON fills
    FOR EACH ROW EXECUTE FUNCTION tq_reject_mutation();

-- The migration log is append-only too: a schema change is a new, reviewed migration.
DROP TRIGGER IF EXISTS migrations_append_only ON schema_migrations;
CREATE TRIGGER migrations_append_only
    BEFORE UPDATE OR DELETE ON schema_migrations
    FOR EACH ROW EXECUTE FUNCTION tq_reject_mutation();
