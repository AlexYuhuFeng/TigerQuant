"""PostgreSQL implementations of the projection repository and the worker lease.

Imports ``psycopg`` lazily and raises :class:`~tigerquant.core.errors.IntegrationUnavailable`
when the ``db`` extra is absent, so importing :mod:`tigerquant.db` never requires a driver
(ADR-0001 §6, AGENTS.md invariant 11).

Two implementation details carry the safety properties from ADR-0013:

* **The lease is acquired in one statement.** ``INSERT ... ON CONFLICT DO UPDATE ... WHERE
  <expired>`` is atomic, so two processes racing for the same mode cannot both see success.
  A read-then-write would leave exactly the window that acceptance gate 8 tests.
* **Health is a real query.** ``health()`` issues ``SELECT 1`` plus a schema-version read, so
  an unreachable or unmigrated database is detected rather than assumed away — which is what
  gate 15 (database outage must stop new exposure) depends on.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from tigerquant.core.canonical import canonical_json_bytes
from tigerquant.core.clock import ensure_utc
from tigerquant.core.enums import Mode
from tigerquant.core.errors import IntegrationUnavailable, JournalIntegrityError
from tigerquant.db.lease import DEFAULT_LEASE_SECONDS, Lease, LeaseError, LeaseState
from tigerquant.db.repository import (
    AlertRow,
    DecisionRow,
    PortfolioStateRow,
    RepositoryHealth,
    RiskOutcomeRow,
)

if TYPE_CHECKING:
    from tigerquant.events.base import EventBase
    from tigerquant.evidence.facts import Fact
    from tigerquant.execution.intent import OrderSnapshot
    from tigerquant.portfolio.ledger import Position

__all__ = ["PostgresLease", "PostgresRepository", "connect"]

#: The extra that provides the driver.
DB_EXTRA = "db"


def _load_driver() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise IntegrationUnavailable("PostgreSQL storage", DB_EXTRA) from exc
    return psycopg


def connect(dsn: str, *, autocommit: bool = False) -> Any:
    """Open a connection. The DSN is read from the environment by the caller, never logged."""
    psycopg = _load_driver()
    return psycopg.connect(dsn, autocommit=autocommit)


def jsonb(value: object) -> Any:
    """Adapt a Python mapping for a ``jsonb`` column.

    psycopg 3 does not adapt ``dict`` implicitly (it cannot know the target type), so an
    unadapted mapping fails with "cannot adapt type 'dict'". Wrapping is explicit and keeps
    the column type in the SQL rather than inferring it from the value.
    """
    from psycopg.types.json import Jsonb

    return Jsonb(value)


class PostgresRepository:
    """Projection storage backed by PostgreSQL.

    The connection is supplied by the caller so that connection policy (pooling, TLS,
    credentials from a secret manager) stays outside this class.
    """

    __slots__ = ("_connection", "_mode")

    def __init__(self, connection: Any, *, mode: Mode) -> None:
        """Bind the repository to one connection **and one mode**.

        The mode is not a column: the database boundary *is* the isolation mechanism
        (ADR-0013 §2), so storing it again would invite a `WHERE mode = ...` filter that a
        future query could forget. The repository stamps it back onto events on read, which
        is the one place it must be reintroduced because events carry it in their envelope.
        """
        self._connection = connection
        self._mode = mode

    @property
    def mode(self) -> Mode:
        return self._mode

    # -- health ------------------------------------------------------------------

    def health(self) -> RepositoryHealth:
        try:
            cursor = self._connection.execute("SELECT 1")
            cursor.fetchone()
            version_row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
            return RepositoryHealth(reachable=False, detail=f"{type(exc).__name__}: {exc}")
        return RepositoryHealth(
            reachable=True,
            detail="postgresql",
            schema_version=int(version_row[0]) if version_row else 0,
        )

    # -- events ------------------------------------------------------------------

    def append_event(self, event: EventBase) -> bool:
        """Store an event idempotently, refusing a sequence fork.

        The insert deliberately omits a conflict target. Naming ``event_id`` as the arbiter
        would raise on a primary-key collision instead of ignoring it, because PostgreSQL
        only consults the index named in the target — so a replay of the same event (same id
        *and* same seq) failed with a UniqueViolation rather than being idempotent.

        With an untargeted ``DO NOTHING`` the duplicate is silent, so the distinction is
        re-established explicitly: if nothing was inserted, either the event is already
        present (fine, idempotent replay) or the sequence belongs to a *different* event
        (a fork, which must be loud). The in-memory implementation enforces the same rule.
        """
        cursor = self._connection.execute(
            """
            INSERT INTO events (
                seq, event_id, event_type, schema_version, occurred_at, recorded_at,
                correlation_id, causation_id, payload, prev_hash, hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING seq
            """,
            (
                event.seq,
                event.event_id,
                event.event_type,
                event.schema_version,
                event.occurred_at,
                event.recorded_at,
                event.correlation_id,
                event.causation_id,
                jsonb(json.loads(canonical_json_bytes(event.payload()).decode("utf-8"))),
                # The chain fields belong to the journal record; the database stores them so
                # a drift check can compare the projection against the JSONL journal.
                getattr(event, "prev_hash", f"sha256:{'0' * 64}"),
                getattr(event, "hash", f"sha256:{'0' * 64}"),
            ),
        )
        if cursor.fetchone() is not None:
            return True

        existing = self._connection.execute(
            "SELECT event_id FROM events WHERE seq = %s", (event.seq,)
        ).fetchone()
        if existing is None:
            # Nothing inserted and nothing at that sequence: the row vanished mid-statement,
            # which only a concurrent truncate could do.
            msg = f"event {event.event_id} was not stored and sequence {event.seq} is empty"
            raise JournalIntegrityError(msg)
        if existing[0] != event.event_id:
            msg = (
                f"event sequence {event.seq} is already occupied by {existing[0]}; the "
                "projection store must not fork"
            )
            raise JournalIntegrityError(msg)
        return False

    def events(self, *, limit: int | None = None) -> tuple[EventBase, ...]:
        """Read events back with their full envelope, in sequence order."""
        from tigerquant.events.registry import parse_event

        columns = (
            "seq, event_id, event_type, schema_version, occurred_at, recorded_at, "
            "correlation_id, causation_id, payload"
        )
        sql = f"SELECT {columns} FROM events ORDER BY seq"  # noqa: S608 - fixed column list
        params: tuple[object, ...] = ()
        if limit is not None:
            sql = (
                f"SELECT {columns} FROM ("  # noqa: S608
                f"  SELECT {columns} FROM events ORDER BY seq DESC LIMIT %s"
                ") AS recent ORDER BY seq"
            )
            params = (limit,)
        cursor = self._connection.execute(sql, params)
        events = []
        for row in cursor.fetchall():
            events.append(
                parse_event(
                    {
                        "seq": row[0],
                        "event_id": row[1],
                        "event_type": row[2],
                        "schema_version": row[3],
                        "occurred_at": row[4],
                        "recorded_at": row[5],
                        "correlation_id": row[6],
                        "causation_id": row[7],
                        # The database is the mode boundary, so the mode is stamped back on
                        # rather than stored (see __init__).
                        "mode": self._mode.value,
                        **(row[8] or {}),
                    }
                )
            )
        return tuple(events)

    def event_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row else 0

    # -- orders and fills --------------------------------------------------------

    def upsert_order(self, snapshot: OrderSnapshot, *, seq: int) -> None:
        self._connection.execute(
            """
            INSERT INTO orders (
                intent_id, decision_id, instrument, side, quantity, filled_quantity,
                remaining_quantity, order_type, time_in_force, limit_price, client_order_id,
                status, broker_order_id, average_fill_price, commission, attempts,
                reason_code, updated_seq
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (intent_id) DO UPDATE SET
                filled_quantity    = EXCLUDED.filled_quantity,
                remaining_quantity = EXCLUDED.remaining_quantity,
                status             = EXCLUDED.status,
                broker_order_id    = EXCLUDED.broker_order_id,
                average_fill_price = EXCLUDED.average_fill_price,
                commission         = EXCLUDED.commission,
                attempts           = EXCLUDED.attempts,
                reason_code        = EXCLUDED.reason_code,
                updated_seq        = EXCLUDED.updated_seq,
                updated_at         = now()
            """,
            (
                snapshot.intent_id,
                snapshot.decision_id,
                snapshot.instrument,
                snapshot.side.value,
                snapshot.quantity,
                snapshot.filled_quantity,
                snapshot.remaining_quantity,
                snapshot.order_type.value,
                snapshot.time_in_force.value,
                snapshot.limit_price,
                snapshot.client_order_id,
                snapshot.status.value,
                snapshot.broker_order_id,
                snapshot.average_fill_price,
                snapshot.commission,
                snapshot.attempts,
                snapshot.reason_code.value if snapshot.reason_code else None,
                seq,
            ),
        )

    def order(self, intent_id: str) -> OrderSnapshot | None:
        from tigerquant.core.enums import OrderSide, OrderStatus, OrderType, ReasonCode, TimeInForce
        from tigerquant.execution.intent import OrderSnapshot

        row = self._connection.execute(
            """
            SELECT intent_id, decision_id, instrument, side, quantity, filled_quantity,
                   remaining_quantity, order_type, time_in_force, limit_price,
                   client_order_id, status, broker_order_id, average_fill_price, commission,
                   attempts, reason_code
            FROM orders WHERE intent_id = %s
            """,
            (intent_id,),
        ).fetchone()
        if row is None:
            return None
        return OrderSnapshot(
            intent_id=row[0],
            decision_id=row[1],
            instrument=row[2],
            side=OrderSide(row[3]),
            quantity=row[4],
            filled_quantity=row[5],
            remaining_quantity=row[6],
            order_type=OrderType(row[7]),
            time_in_force=TimeInForce(row[8]),
            limit_price=row[9],
            client_order_id=row[10],
            status=OrderStatus(row[11]),
            broker_order_id=row[12],
            average_fill_price=row[13],
            commission=row[14],
            attempts=row[15],
            reason_code=ReasonCode(row[16]) if row[16] else None,
        )

    def open_orders(self) -> tuple[OrderSnapshot, ...]:
        cursor = self._connection.execute(
            "SELECT intent_id FROM orders WHERE status NOT IN ('FILLED','CANCELED','REJECTED') "
            "ORDER BY intent_id"
        )
        out = []
        for (intent_id,) in cursor.fetchall():
            snapshot = self.order(intent_id)
            if snapshot is not None:
                out.append(snapshot)
        return tuple(out)

    def append_fill(
        self, *, exec_id: str, intent_id: str, seq: int, payload: dict[str, object]
    ) -> bool:
        cursor = self._connection.execute(
            """
            INSERT INTO fills (exec_id, intent_id, instrument, side, quantity, price,
                               commission, occurred_at, seq)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (exec_id) DO NOTHING
            RETURNING exec_id
            """,
            (
                exec_id,
                intent_id,
                payload["instrument"],
                payload["side"],
                payload["quantity"],
                payload["price"],
                payload.get("commission", 0),
                payload["occurred_at"],
                seq,
            ),
        )
        return cursor.fetchone() is not None

    def fill_count(self, intent_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM fills WHERE intent_id = %s", (intent_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    # -- portfolio ---------------------------------------------------------------

    def upsert_position(self, position: Position, *, seq: int) -> None:
        # The CHECK constraint on positions.quantity enforces long-only in the database as
        # well as in Python; a negative value raises there.
        self._connection.execute(
            """
            INSERT INTO positions (instrument, quantity, average_cost, realized_pnl, updated_seq)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (instrument) DO UPDATE SET
                quantity     = EXCLUDED.quantity,
                average_cost = EXCLUDED.average_cost,
                realized_pnl = EXCLUDED.realized_pnl,
                updated_seq  = EXCLUDED.updated_seq,
                updated_at   = now()
            """,
            (
                position.instrument,
                position.quantity,
                position.average_cost,
                position.realized_pnl,
                seq,
            ),
        )

    def positions(self) -> tuple[Position, ...]:
        from tigerquant.portfolio.ledger import Position

        cursor = self._connection.execute(
            "SELECT instrument, quantity, average_cost, realized_pnl FROM positions "
            "ORDER BY instrument"
        )
        return tuple(
            Position(
                instrument=row[0],
                quantity=row[1],
                average_cost=row[2],
                realized_pnl=row[3],
            )
            for row in cursor.fetchall()
        )

    def set_portfolio_state(self, state: PortfolioStateRow, *, seq: int) -> None:
        self._connection.execute(
            """
            INSERT INTO portfolio_state (singleton, cash, equity, gross_exposure,
                                         net_exposure, ledger_digest, updated_seq, as_of)
            VALUES (true, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (singleton) DO UPDATE SET
                cash           = EXCLUDED.cash,
                equity         = EXCLUDED.equity,
                gross_exposure = EXCLUDED.gross_exposure,
                net_exposure   = EXCLUDED.net_exposure,
                ledger_digest  = EXCLUDED.ledger_digest,
                updated_seq    = EXCLUDED.updated_seq,
                as_of          = EXCLUDED.as_of
            """,
            (
                state.cash,
                state.equity,
                state.gross_exposure,
                state.net_exposure,
                state.ledger_digest,
                seq,
                state.as_of,
            ),
        )

    def portfolio_state(self) -> PortfolioStateRow | None:
        row = self._connection.execute(
            "SELECT cash, equity, gross_exposure, net_exposure, ledger_digest, updated_seq, as_of "
            "FROM portfolio_state WHERE singleton"
        ).fetchone()
        if row is None:
            return None
        return PortfolioStateRow(
            cash=row[0],
            equity=row[1],
            gross_exposure=row[2],
            net_exposure=row[3],
            ledger_digest=row[4],
            updated_seq=row[5],
            as_of=row[6],
        )

    # -- evidence ----------------------------------------------------------------

    def add_fact(self, fact: Fact) -> bool:
        cursor = self._connection.execute(
            """
            INSERT INTO evidence_facts (
                fact_id, kind, category, instrument, event_time, published_at, available_at,
                ingested_at, valid_until, source_id, content_hash, license_scope,
                quality_flags, supersedes, attributes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fact_id) DO NOTHING
            RETURNING fact_id
            """,
            (
                fact.fact_id,
                fact.kind.value,
                fact.category,
                fact.instrument,
                fact.event_time,
                fact.published_at,
                fact.available_at,
                fact.ingested_at,
                fact.valid_until,
                fact.source_id,
                fact.content_hash,
                fact.license_scope,
                list(fact.quality_flags),
                fact.supersedes,
                jsonb(json.loads(canonical_json_bytes(fact.attributes).decode("utf-8"))),
            ),
        )
        return cursor.fetchone() is not None

    def facts_as_of(self, instant: dt.datetime, *, category: str | None = None) -> tuple[Fact, ...]:
        from tigerquant.evidence.facts import Fact, FactKind

        sql = (
            "SELECT fact_id, kind, category, instrument, event_time, published_at, "
            "available_at, ingested_at, valid_until, source_id, content_hash, license_scope, "
            "quality_flags, supersedes, attributes FROM evidence_facts WHERE available_at <= %s"
        )
        params: list[object] = [ensure_utc(instant)]
        if category is not None:
            sql += " AND category = %s"
            params.append(category)
        sql += " ORDER BY available_at, fact_id"
        cursor = self._connection.execute(sql, tuple(params))
        return tuple(
            Fact(
                fact_id=row[0],
                kind=FactKind(row[1]),
                category=row[2],
                instrument=row[3],
                event_time=row[4],
                published_at=row[5],
                available_at=row[6],
                ingested_at=row[7],
                valid_until=row[8],
                source_id=row[9],
                content_hash=row[10],
                license_scope=row[11],
                quality_flags=tuple(row[12] or ()),
                supersedes=row[13],
                attributes=dict(row[14] or {}),
            )
            for row in cursor.fetchall()
        )

    # -- lineage -----------------------------------------------------------------

    def record_decision(self, row: DecisionRow, *, seq: int) -> None:
        self._connection.execute(
            """
            INSERT INTO decisions (
                decision_id, snapshot_id, model_version, prompt_version, action, instrument,
                target_weight, horizon, thesis, counter_thesis, invalidation, evidence_ids,
                uncertainties, seq, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (decision_id) DO NOTHING
            """,
            (
                row.decision_id,
                row.snapshot_id,
                row.model_version,
                row.prompt_version,
                row.action,
                row.instrument,
                row.target_weight,
                row.horizon,
                row.thesis,
                row.counter_thesis,
                row.invalidation,
                list(row.evidence_ids),
                list(row.uncertainties),
                seq,
                row.occurred_at,
            ),
        )

    def record_risk_outcome(self, row: RiskOutcomeRow) -> None:
        # The risk_deny_has_reason CHECK refuses a DENY without a reason code, so a bug in
        # the risk plane cannot persist an unexplained refusal.
        self._connection.execute(
            """
            INSERT INTO risk_outcomes (seq, intent_id, outcome, reason_codes, policy_hash,
                                       portfolio_digest, checks_run, detail, occurred_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (seq) DO NOTHING
            """,
            (
                row.seq,
                row.intent_id,
                row.outcome,
                list(row.reason_codes),
                row.policy_hash,
                row.portfolio_digest,
                list(row.checks_run),
                row.detail,
                row.occurred_at,
            ),
        )

    def add_alert(self, row: AlertRow) -> None:
        self._connection.execute(
            """
            INSERT INTO alerts (seq, severity, code, message, occurrences, context, occurred_at)
            VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s)
            ON CONFLICT (seq) DO NOTHING
            """,
            (row.seq, row.severity, row.code, row.message, row.occurrences, row.occurred_at),
        )

    def alerts(self, *, limit: int = 100) -> tuple[AlertRow, ...]:
        cursor = self._connection.execute(
            "SELECT seq, severity, code, message, occurrences, occurred_at FROM alerts "
            "ORDER BY seq DESC LIMIT %s",
            (limit,),
        )
        rows = [
            AlertRow(
                seq=row[0],
                severity=row[1],
                code=row[2],
                message=row[3],
                occurrences=row[4],
                occurred_at=row[5],
            )
            for row in cursor.fetchall()
        ]
        return tuple(reversed(rows))


class PostgresLease:
    """Worker lease stored in the mode's own database.

    Acquisition is a single atomic statement: the row is inserted, or updated only when the
    existing lease has expired. Two concurrent acquirers therefore cannot both succeed.

    **Lease operations commit immediately**, unlike the projection repository. This is not an
    inconsistency, it is the point: a lease held inside an uncommitted transaction is
    invisible to every other worker, so mutual exclusion would not exist, and a second
    acquirer would *block on the row lock* rather than be refused. Committing here makes the
    lease a piece of coordination state that is true the moment it is taken, independent of
    whatever data transaction the caller is in.
    """

    __slots__ = ("_connection", "_name", "_seconds")

    def __init__(
        self,
        connection: Any,
        name: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if lease_seconds < 1:
            msg = "lease_seconds must be positive"
            raise LeaseError(msg)
        self._connection = connection
        self._name = name
        self._seconds = lease_seconds

    def _commit(self) -> None:
        commit = getattr(self._connection, "commit", None)
        if callable(commit):
            commit()

    @property
    def name(self) -> str:
        return self._name

    def state(self) -> LeaseState:
        row = self._connection.execute(
            "SELECT owner, acquired_at, heartbeat_at, expires_at FROM worker_leases "
            "WHERE lease_name = %s",
            (self._name,),
        ).fetchone()
        if row is None:
            return LeaseState(
                name=self._name, owner=None, acquired_at=None, heartbeat_at=None, expires_at=None
            )
        return LeaseState(
            name=self._name,
            owner=row[0],
            acquired_at=row[1],
            heartbeat_at=row[2],
            expires_at=row[3],
        )

    def acquire(self, owner: str) -> Lease | None:
        if not owner:
            msg = "lease owner must be non-empty; an anonymous lease cannot be released"
            raise LeaseError(msg)
        previous = self.state().owner
        row = self._connection.execute(
            """
            INSERT INTO worker_leases (lease_name, owner, acquired_at, heartbeat_at, expires_at)
            VALUES (%s, %s, now(), now(), now() + make_interval(secs => %s))
            ON CONFLICT (lease_name) DO UPDATE SET
                owner        = EXCLUDED.owner,
                acquired_at  = EXCLUDED.acquired_at,
                heartbeat_at = EXCLUDED.heartbeat_at,
                expires_at   = EXCLUDED.expires_at
            WHERE worker_leases.expires_at <= now()
            RETURNING owner, acquired_at, expires_at
            """,
            (self._name, owner, self._seconds),
        ).fetchone()
        self._commit()
        if row is None:
            return None
        return Lease(
            name=self._name,
            owner=row[0],
            acquired_at=row[1],
            expires_at=row[2],
            previous_owner=previous if previous != owner else None,
        )

    def heartbeat(self, owner: str) -> bool:
        row = self._connection.execute(
            """
            UPDATE worker_leases
               SET heartbeat_at = now(),
                   expires_at   = now() + make_interval(secs => %s)
             WHERE lease_name = %s AND owner = %s AND expires_at > now()
            RETURNING lease_name
            """,
            (self._seconds, self._name, owner),
        ).fetchone()
        self._commit()
        return row is not None

    def release(self, owner: str) -> bool:
        row = self._connection.execute(
            "DELETE FROM worker_leases WHERE lease_name = %s AND owner = %s RETURNING lease_name",
            (self._name, owner),
        ).fetchone()
        self._commit()
        return row is not None

    def describe(self) -> str:
        state = self.state()
        if not state.is_held:
            return f"lease {self._name!r} is free"
        return f"lease {self._name!r} held by {state.owner}"
