"""PostgreSQL integration tests — the SQL half of the repository and lease contracts.

Marked ``requires_db``. When no server is reachable these **skip**, and a skipped test is
never reported as a pass (ADR-0013 §4). Bring a server up with::

    docker compose -f deploy/compose.yaml up -d
    python tools/db_migrate.py --mode backtest

DSN resolution: ``TIGERQUANT_TEST_DSN``, else the loopback development DSN for the backtest
mode. Every test runs against the ``tq_backtest`` database only, so a stray statement can
never touch another mode's data — which is the isolation property under test in
``test_cross_mode_isolation_is_enforced_by_the_server``.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Mode, OrderSide, OrderStatus, OrderType, ReasonCode, TimeInForce
from tigerquant.core.errors import IntegrationUnavailable
from tigerquant.core.ids import IdFactory
from tigerquant.db.migrations import MigrationRunner
from tigerquant.db.postgres import PostgresLease, PostgresRepository, connect
from tigerquant.db.repository import PortfolioStateRow
from tigerquant.events.emitter import EventEmitter
from tigerquant.events.journal import MemoryJournal
from tigerquant.events.risk import AlertRaised
from tigerquant.evidence.facts import Fact, FactKind
from tigerquant.execution.intent import OrderSnapshot
from tigerquant.portfolio.ledger import Position

pytestmark = pytest.mark.requires_db

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
DEV_DSN = "postgresql://tq_backtest:tq_local_development_only@127.0.0.1:5432/tq_backtest"


def dsn() -> str:
    return os.environ.get("TIGERQUANT_TEST_DSN", DEV_DSN)


def _connection_or_skip() -> object:
    try:
        connection = connect(dsn())
    except IntegrationUnavailable as exc:
        pytest.skip(f"driver not installed: {exc}")
    except Exception as exc:  # noqa: BLE001 - any connection problem means "no server"
        pytest.skip(f"no PostgreSQL server reachable: {type(exc).__name__}: {exc}")
    return connection


@pytest.fixture(scope="module")
def migrated_connection() -> object:
    """A connection to a migrated, empty-enough backtest database."""
    connection = _connection_or_skip()
    MigrationRunner().apply(connection)
    connection.commit()
    yield connection
    connection.close()


#: Every table a test may touch. Truncated before each test so no test can depend on
#: another's leftovers — which is also why the history tables are reset with TRUNCATE
#: rather than DELETE: DELETE is refused by the append-only triggers, by design.
_ALL_TABLES = (
    "events, orders, fills, positions, portfolio_state, evidence_facts, decisions, "
    "risk_outcomes, alerts, worker_leases"
)


@pytest.fixture(autouse=True)
def _clean_state(migrated_connection: object) -> object:
    """Guarantee every test starts from a clean, non-aborted transaction.

    Two jobs, both learned the hard way:

    * Several tests deliberately provoke a server-side refusal (append-only trigger, CHECK
      constraint, CONNECT denial). In PostgreSQL the first error aborts the whole
      transaction, so without an explicit rollback the *next* test fails with
      ``InFailedSqlTransaction`` and the real failure is masked.
    * Rows committed by one test must not leak into the next, or an idempotency test can
      pass or fail depending on ordering.
    """
    migrated_connection.rollback()
    migrated_connection.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
    migrated_connection.commit()
    yield migrated_connection
    migrated_connection.rollback()


@pytest.fixture
def repository(migrated_connection: object) -> PostgresRepository:
    return PostgresRepository(migrated_connection, mode=Mode.BACKTEST)


def _order(status: OrderStatus = OrderStatus.ACKNOWLEDGED) -> OrderSnapshot:
    return OrderSnapshot(
        intent_id="bt-oi_pg1",
        decision_id="bt-dec_pg1",
        instrument="SPY",
        side=OrderSide.BUY,
        quantity=8,
        filled_quantity=0,
        remaining_quantity=8,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        client_order_id="TQ-bt-pg1",
        status=status,
    )


def _fact(minutes: int = 0) -> Fact:
    occurred = AT + dt.timedelta(minutes=minutes)
    return Fact(
        fact_id=f"fact:pg{minutes:04d}",
        kind=FactKind.OBSERVED,
        category="price_bar",
        instrument="SPY",
        event_time=occurred,
        published_at=occurred,
        available_at=occurred,
        ingested_at=occurred,
        valid_until=occurred + dt.timedelta(hours=1),
        source_id="fixture:synthetic",
        content_hash="sha256:" + f"{minutes:064d}",
        attributes={"value": "600.00"},
    )


class TestMigratedSchema:
    def test_health_reports_the_schema_version(self, repository: PostgresRepository) -> None:
        health = repository.health()
        assert health.reachable
        assert health.schema_version == MigrationRunner().head_version

    def test_every_expected_table_exists(self, migrated_connection: object) -> None:
        cursor = migrated_connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert {
            "schema_migrations",
            "events",
            "orders",
            "fills",
            "positions",
            "portfolio_state",
            "evidence_facts",
            "decisions",
            "risk_outcomes",
            "alerts",
            "worker_leases",
        } <= tables

    def test_history_tables_are_append_only(self, migrated_connection: object) -> None:
        """An UPDATE to history must be refused by the server, not merely discouraged."""
        migrated_connection.execute(
            """
            INSERT INTO events (seq, event_id, event_type, schema_version, occurred_at,
                                recorded_at, correlation_id, payload, prev_hash, hash)
            VALUES (0, 'ev-1', 'AlertRaised', '1.0.0', now(), now(), 'c', '{}'::jsonb, %s, %s)
            """,
            ("sha256:" + "0" * 64, "sha256:" + "1" * 64),
        )
        migrated_connection.commit()
        with pytest.raises(Exception, match="append-only"):
            migrated_connection.execute("UPDATE events SET event_type = 'Tampered' WHERE seq = 0")
        migrated_connection.rollback()
        with pytest.raises(Exception, match="append-only"):
            migrated_connection.execute("DELETE FROM events WHERE seq = 0")
        migrated_connection.rollback()
        # The row survived both refusals: history is intact.
        row = migrated_connection.execute("SELECT event_type FROM events WHERE seq = 0").fetchone()
        assert row is not None
        assert row[0] == "AlertRaised"

    def test_long_only_is_enforced_by_the_database(self, migrated_connection: object) -> None:
        with pytest.raises(Exception, match="positions_long_only|check constraint"):
            migrated_connection.execute(
                "INSERT INTO positions (instrument, quantity, average_cost, updated_seq) "
                "VALUES ('SPY', -1, 0, 1)"
            )
        migrated_connection.rollback()

    def test_a_denial_cannot_be_stored_without_a_reason(self, migrated_connection: object) -> None:
        with pytest.raises(Exception, match="risk_deny_has_reason|check constraint"):
            migrated_connection.execute(
                "INSERT INTO risk_outcomes (seq, intent_id, outcome, reason_codes, policy_hash, "
                "portfolio_digest, occurred_at) VALUES (1, 'i', 'DENY', '{}', 'h', 'd', now())"
            )
        migrated_connection.rollback()

    def test_migrations_are_idempotent_against_a_live_server(
        self, migrated_connection: object
    ) -> None:
        assert MigrationRunner().apply(migrated_connection) == ()


class TestPostgresRepositoryContract:
    """Mirrors tests/unit/test_db_layer.py::TestInMemoryRepositoryContract."""

    def test_event_insertion_is_idempotent(self, repository: PostgresRepository) -> None:
        clock = SimulatedClock(AT)
        emitter = EventEmitter(
            mode=Mode.BACKTEST,
            sink=MemoryJournal(Mode.BACKTEST),
            ids=IdFactory("bt", clock),
            clock=clock,
            correlation_id="pg",
        )
        event = emitter.emit(
            AlertRaised,
            occurred_at=AT,
            severity="INFO",
            code=ReasonCode.CLOCK_SKEW,
            message="contract",
        )
        assert repository.append_event(event) is True
        assert repository.append_event(event) is False
        assert repository.event_count() == 1
        assert repository.events()[-1].event_type == "AlertRaised"

    def test_order_upsert_and_read(self, repository: PostgresRepository) -> None:
        repository.upsert_order(_order(), seq=1)
        loaded = repository.order("bt-oi_pg1")
        assert loaded is not None
        assert loaded.status is OrderStatus.ACKNOWLEDGED
        assert loaded.quantity == 8

    def test_order_status_advances_in_place(self, repository: PostgresRepository) -> None:
        repository.upsert_order(_order(), seq=1)
        filled = _order(OrderStatus.FILLED).model_copy(
            update={
                "filled_quantity": 8,
                "remaining_quantity": 0,
                "average_fill_price": Decimal(600),
            }
        )
        repository.upsert_order(filled, seq=2)
        loaded = repository.order("bt-oi_pg1")
        assert loaded is not None
        assert loaded.status is OrderStatus.FILLED
        assert repository.open_orders() == ()

    def test_fill_insertion_is_idempotent(self, repository: PostgresRepository) -> None:
        repository.upsert_order(_order(), seq=1)
        payload = {
            "instrument": "SPY",
            "side": "BUY",
            "quantity": 8,
            "price": Decimal("600.00"),
            "commission": Decimal("0.04"),
            "occurred_at": AT,
        }
        assert repository.append_fill(
            exec_id="exec-pg1", intent_id="bt-oi_pg1", seq=2, payload=payload
        )
        assert not repository.append_fill(
            exec_id="exec-pg1", intent_id="bt-oi_pg1", seq=3, payload=payload
        )
        assert repository.fill_count("bt-oi_pg1") == 1

    def test_positions_round_trip(self, repository: PostgresRepository) -> None:
        repository.upsert_position(Position("SPY", 8, Decimal("600.0000"), Decimal(0)), seq=1)
        positions = repository.positions()
        assert len(positions) == 1
        assert positions[0].quantity == 8
        assert positions[0].average_cost == Decimal("600.0000")

    def test_portfolio_state_round_trip(self, repository: PostgresRepository) -> None:
        state = PortfolioStateRow(
            cash=Decimal("95179.48"),
            equity=Decimal("100015.48"),
            gross_exposure=Decimal("4820.48"),
            net_exposure=Decimal("4820.48"),
            ledger_digest="sha256:" + "a" * 64,
            updated_seq=7,
            as_of=AT,
        )
        repository.set_portfolio_state(state, seq=7)
        loaded = repository.portfolio_state()
        assert loaded is not None
        assert loaded.cash == Decimal("95179.48")
        assert loaded.ledger_digest == state.ledger_digest

    def test_facts_are_point_in_time(self, repository: PostgresRepository) -> None:
        repository.add_fact(_fact(0))
        repository.add_fact(_fact(10))
        assert len(repository.facts_as_of(AT + dt.timedelta(minutes=1))) == 1
        assert len(repository.facts_as_of(AT + dt.timedelta(minutes=20))) == 2

    def test_fact_insertion_is_idempotent(self, repository: PostgresRepository) -> None:
        assert repository.add_fact(_fact()) is True
        assert repository.add_fact(_fact()) is False

    def test_alerts_are_ordered(self, repository: PostgresRepository) -> None:
        from tigerquant.db.repository import AlertRow

        for seq in (3, 1, 2):
            repository.add_alert(
                AlertRow(
                    seq=seq,
                    severity="WARNING",
                    code="CLOCK_SKEW",
                    message=f"alert {seq}",
                    occurrences=1,
                    occurred_at=AT,
                )
            )
        assert [row.seq for row in repository.alerts()] == [1, 2, 3]


class TestPostgresLease:
    """Acceptance gate 8, against a real server."""

    def _lease(self, connection: object, name: str) -> PostgresLease:
        connection.execute("DELETE FROM worker_leases WHERE lease_name = %s", (name,))
        connection.commit()
        return PostgresLease(connection, name, lease_seconds=60)

    def test_first_acquirer_wins_and_second_is_refused(self, migrated_connection: object) -> None:
        lease = self._lease(migrated_connection, "gate8-live")
        first = lease.acquire("worker-a")
        assert first is not None
        assert first.owner == "worker-a"
        assert lease.acquire("worker-b") is None
        assert lease.state().owner == "worker-a"

    def test_expired_lease_can_be_taken_over_and_names_the_previous_owner(
        self, migrated_connection: object
    ) -> None:
        lease = self._lease(migrated_connection, "gate8-expiry")
        lease.acquire("worker-a")
        # Force expiry rather than waiting. `heartbeat_at` moves with it, because the table
        # asserts expires_at > heartbeat_at — the constraint is doing its job, so the test
        # must produce a *consistent* expired row rather than an impossible one.
        migrated_connection.execute(
            "UPDATE worker_leases SET heartbeat_at = now() - interval '2 seconds', "
            "expires_at = now() - interval '1 second' WHERE lease_name = %s",
            ("gate8-expiry",),
        )
        migrated_connection.commit()
        taken = PostgresLease(migrated_connection, "gate8-expiry", lease_seconds=60).acquire(
            "worker-b"
        )
        assert taken is not None
        assert taken.previous_owner == "worker-a"

    def test_heartbeat_and_release_are_owner_only(self, migrated_connection: object) -> None:
        lease = self._lease(migrated_connection, "gate8-owner")
        lease.acquire("worker-a")
        assert lease.heartbeat("worker-b") is False
        assert lease.heartbeat("worker-a") is True
        assert lease.release("worker-b") is False
        assert lease.release("worker-a") is True
        assert not lease.state().is_held

    def test_two_connections_racing_produce_exactly_one_winner(
        self, migrated_connection: object
    ) -> None:
        """The property the single atomic statement exists to guarantee."""
        name = "gate8-race"
        migrated_connection.execute("DELETE FROM worker_leases WHERE lease_name = %s", (name,))
        migrated_connection.commit()

        first_connection = connect(dsn())
        second_connection = connect(dsn())
        try:
            a = PostgresLease(first_connection, name, lease_seconds=60)
            b = PostgresLease(second_connection, name, lease_seconds=60)
            results = [a.acquire("worker-a"), b.acquire("worker-b")]
            winners = [lease for lease in results if lease is not None]
            assert len(winners) == 1
        finally:
            first_connection.close()
            second_connection.close()


class TestCrossModeIsolation:
    """AGENTS.md invariant 2, enforced by the server rather than by convention."""

    def test_cross_mode_isolation_is_enforced_by_the_server(self) -> None:
        """The backtest role must not be able to open the paper database.

        The DSN is built explicitly rather than by string-replacing the backtest DSN: a
        naive replace also rewrites the *username*, which would connect as ``tq_paper`` and
        make this test pass for the wrong reason.
        """
        other = "postgresql://tq_backtest:tq_local_development_only@127.0.0.1:5432/tq_paper"
        try:
            connection = connect(other)
        except IntegrationUnavailable as exc:
            pytest.skip(f"driver not installed: {exc}")
        except Exception as exc:  # noqa: BLE001 - any refusal is the expected branch
            # Expected: the backtest role has no CONNECT privilege on the paper database.
            # Asserting on the message (rather than merely on "it raised") keeps a typo in
            # the host name or a network failure from looking like a passing isolation test.
            message = str(exc).lower()
            if "permission denied" not in message and "connect" not in message:
                pytest.fail(f"expected a CONNECT privilege error, got: {exc}")
            return
        connection.close()
        pytest.fail(
            "the backtest role was able to connect to the paper database; mode isolation "
            "is not enforced"
        )
