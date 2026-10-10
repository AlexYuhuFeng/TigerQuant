"""Persistence layer: migration runner, projection repository and worker lease.

The behavioural suites here are written to run against **both** implementations of each
contract. The PostgreSQL halves are marked ``requires_db`` and are skipped when no server is
available, so a green offline run never implies the SQL was exercised (ADR-0013 §4).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from tigerquant.core.enums import Mode, OrderSide, OrderStatus, OrderType, ReasonCode, TimeInForce
from tigerquant.core.errors import JournalIntegrityError
from tigerquant.db.lease import InMemoryLease, LeaseError
from tigerquant.db.migrations import (
    MigrationError,
    MigrationRunner,
    content_digest,
    discover_migrations,
)
from tigerquant.db.repository import (
    AlertRow,
    InMemoryRepository,
    PortfolioStateRow,
)
from tigerquant.evidence.facts import Fact, FactKind
from tigerquant.execution.intent import OrderSnapshot
from tigerquant.portfolio.ledger import Position

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# A fake DB-API connection: enough of the interface for the migration runner, and it
# records every statement so the runner's ordering and idempotency are observable.
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self._rows = rows or []

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class FakeConnection:
    """Minimal DB-API surface, plus fault injection for rollback testing."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.parameters: list[object] = []
        self.applied: list[tuple[int, str, str]] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on: str | None = None

    def execute(self, sql: str, params: object = ()) -> FakeCursor:
        self.statements.append(sql.strip())
        self.parameters.append(params)
        if self.fail_on is not None and self.fail_on in sql:
            msg = f"injected failure for {self.fail_on!r}"
            raise RuntimeError(msg)
        if sql.strip().upper().startswith("SELECT VERSION, NAME, CHECKSUM"):
            return FakeCursor([(v, n, c) for v, n, c in self.applied])
        if "INSERT INTO SCHEMA_MIGRATIONS" in sql.strip().upper():
            version, name, checksum = params  # type: ignore[misc]
            self.applied.append((int(version), str(name), str(checksum)))
        return FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class TestMigrationDiscovery:
    def test_the_repository_ships_at_least_one_migration(self) -> None:
        migrations = discover_migrations()
        assert migrations
        assert migrations[0].version == 1

    def test_migrations_are_ordered_by_version(self) -> None:
        versions = [m.version for m in discover_migrations()]
        assert versions == sorted(versions)

    def test_checksum_is_stable_across_line_endings(self, tmp_path: Path) -> None:
        """A CRLF checkout on Windows must not look like tampering."""
        unix = tmp_path / "0001_a.sql"
        unix.write_bytes(b"SELECT 1;\n")
        first = discover_migrations(tmp_path)[0].checksum
        unix.write_bytes(b"SELECT 1;\r\n")
        assert discover_migrations(tmp_path)[0].checksum == first

    def test_bad_filename_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "initial.sql").write_text("SELECT 1;", encoding="utf-8")
        with pytest.raises(MigrationError, match="does not match"):
            discover_migrations(tmp_path)

    def test_duplicate_version_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp_path / "0001_b.sql").write_text("SELECT 2;", encoding="utf-8")
        with pytest.raises(MigrationError, match="duplicate migration version"):
            discover_migrations(tmp_path)

    def test_empty_migration_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "0001_a.sql").write_text("   \n", encoding="utf-8")
        with pytest.raises(MigrationError, match="is empty"):
            discover_migrations(tmp_path)

    def test_empty_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError, match="no migrations found"):
            discover_migrations(tmp_path)

    def test_content_digest_is_stable(self) -> None:
        assert content_digest(discover_migrations()) == content_digest(discover_migrations())


class TestMigrationRunner:
    def test_fresh_database_applies_everything(self) -> None:
        connection = FakeConnection()
        runner = MigrationRunner()
        applied = runner.apply(connection)
        assert [m.version for m in applied] == [m.version for m in runner.migrations]
        assert connection.rollbacks == 0
        assert connection.commits >= len(applied)

    def test_second_run_is_a_noop(self) -> None:
        connection = FakeConnection()
        runner = MigrationRunner()
        runner.apply(connection)
        before = len(connection.statements)
        again = runner.apply(connection)
        assert again == ()
        # Only the ledger bootstrap and the applied-rows read should have happened.
        assert len(connection.statements) - before <= 2

    def test_dry_run_reports_without_applying(self) -> None:
        connection = FakeConnection()
        runner = MigrationRunner()
        planned = runner.apply(connection, dry_run=True)
        assert planned
        assert connection.applied == []

    def test_edited_history_is_refused(self) -> None:
        """An applied migration whose file changed must stop the runner."""
        connection = FakeConnection()
        runner = MigrationRunner()
        runner.apply(connection)
        poisoned = FakeConnection()
        poisoned.applied = [(row[0], row[1], "sha256:" + "f" * 64) for row in connection.applied]
        with pytest.raises(MigrationError, match="immutable"):
            runner.plan(poisoned)

    def test_unknown_applied_migration_is_refused(self) -> None:
        connection = FakeConnection()
        connection.applied = [(99, "from_the_future", "sha256:" + "a" * 64)]
        with pytest.raises(MigrationError, match="no such migration"):
            MigrationRunner().plan(connection)

    def test_failure_rolls_back_and_names_the_migration(self) -> None:
        connection = FakeConnection()
        connection.fail_on = "CREATE TABLE IF NOT EXISTS events"
        migration = discover_migrations()[0]
        connection.fail_on = "CREATE INDEX"
        with pytest.raises(MigrationError, match=migration.label):
            MigrationRunner().apply(connection)
        assert connection.rollbacks == 1

    def test_status_lists_every_migration(self) -> None:
        connection = FakeConnection()
        runner = MigrationRunner()
        assert "PENDING" in runner.status(connection)
        runner.apply(connection)
        assert "pending" not in runner.status(connection)


class TestInMemoryRepositoryContract:
    """The same behavioural suite is run against Postgres in test_db_postgres.py."""

    @pytest.fixture
    def repository(self) -> InMemoryRepository:
        return InMemoryRepository()

    def test_health_is_reachable(self, repository: InMemoryRepository) -> None:
        health = repository.health()
        assert health.reachable
        assert bool(health)

    def test_outage_is_reported_and_blocks_writes(self, repository: InMemoryRepository) -> None:
        repository.simulate_outage(reachable=False)
        assert not repository.health().reachable
        with pytest.raises(JournalIntegrityError, match="unreachable"):
            repository.add_fact(self._fact())

    def test_event_insertion_is_idempotent(self, repository: InMemoryRepository) -> None:
        from tigerquant.core.clock import SimulatedClock
        from tigerquant.core.enums import Severity
        from tigerquant.core.ids import IdFactory
        from tigerquant.events.emitter import EventEmitter
        from tigerquant.events.journal import MemoryJournal
        from tigerquant.events.risk import AlertRaised

        clock = SimulatedClock(AT)
        emitter = EventEmitter(
            mode=Mode.BACKTEST,
            sink=MemoryJournal(Mode.BACKTEST),
            ids=IdFactory("bt", clock),
            clock=clock,
            correlation_id="db-test",
        )
        event = emitter.emit(
            AlertRaised,
            occurred_at=AT,
            severity=Severity.INFO,
            code=ReasonCode.CLOCK_SKEW,
            message="test",
        )
        assert repository.append_event(event) is True
        assert repository.append_event(event) is False
        assert repository.event_count() == 1

    def test_two_events_cannot_share_a_sequence(self, repository: InMemoryRepository) -> None:
        from tigerquant.core.clock import SimulatedClock
        from tigerquant.core.enums import Severity
        from tigerquant.core.ids import IdFactory
        from tigerquant.events.emitter import EventEmitter
        from tigerquant.events.journal import MemoryJournal
        from tigerquant.events.risk import AlertRaised

        clock = SimulatedClock(AT)
        emitter = EventEmitter(
            mode=Mode.BACKTEST,
            sink=MemoryJournal(Mode.BACKTEST),
            ids=IdFactory("bt", clock),
            clock=clock,
            correlation_id="db-test",
        )
        first = emitter.emit(
            AlertRaised,
            occurred_at=AT,
            severity=Severity.INFO,
            code=ReasonCode.CLOCK_SKEW,
            message="a",
        )
        second = emitter.emit(
            AlertRaised,
            occurred_at=AT,
            severity=Severity.INFO,
            code=ReasonCode.CLOCK_SKEW,
            message="b",
        )
        repository.append_event(first)
        forged = second.model_copy(update={"seq": first.seq, "event_id": "bt-ev_forged"})
        with pytest.raises(JournalIntegrityError, match="must not fork"):
            repository.append_event(forged)

    def test_order_upsert_and_read(self, repository: InMemoryRepository) -> None:
        snapshot = self._order()
        repository.upsert_order(snapshot, seq=1)
        assert repository.order(snapshot.intent_id) == snapshot
        assert len(repository.open_orders()) == 1

    def test_terminal_order_cannot_be_reopened(self, repository: InMemoryRepository) -> None:
        filled = self._order(status=OrderStatus.FILLED)
        repository.upsert_order(filled, seq=1)
        reopened = self._order(status=OrderStatus.ACKNOWLEDGED)
        with pytest.raises(JournalIntegrityError, match="terminal"):
            repository.upsert_order(reopened, seq=2)

    def test_fill_insertion_is_idempotent(self, repository: InMemoryRepository) -> None:
        repository.upsert_order(self._order(), seq=1)
        payload = {
            "instrument": "SPY",
            "side": "BUY",
            "quantity": 8,
            "price": Decimal("600.00"),
            "commission": Decimal("0.04"),
            "occurred_at": AT,
        }
        assert repository.append_fill(exec_id="exec-1", intent_id="bt-oi_1", seq=2, payload=payload)
        assert not repository.append_fill(
            exec_id="exec-1", intent_id="bt-oi_1", seq=3, payload=payload
        )
        assert repository.fill_count("bt-oi_1") == 1

    def test_short_positions_are_refused(self, repository: InMemoryRepository) -> None:
        short = Position("SPY", -5, Decimal("600.00"), Decimal(0))
        with pytest.raises(JournalIntegrityError, match="long-only"):
            repository.upsert_position(short, seq=1)

    def test_positions_are_returned_in_a_stable_order(self, repository: InMemoryRepository) -> None:
        for instrument in ("SPY", "AAPL", "QQQ"):
            repository.upsert_position(
                Position(instrument, 1, Decimal("100.00"), Decimal(0)), seq=1
            )
        assert [p.instrument for p in repository.positions()] == ["AAPL", "QQQ", "SPY"]

    def test_portfolio_state_round_trip(self, repository: InMemoryRepository) -> None:
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
        assert repository.portfolio_state() == state

    def test_facts_are_point_in_time(self, repository: InMemoryRepository) -> None:
        early = self._fact(minutes=0)
        late = self._fact(minutes=10)
        repository.add_fact(early)
        repository.add_fact(late)
        assert repository.facts_as_of(AT + dt.timedelta(minutes=1)) == (early,)
        assert len(repository.facts_as_of(AT + dt.timedelta(minutes=20))) == 2

    def test_fact_insertion_is_idempotent(self, repository: InMemoryRepository) -> None:
        fact = self._fact()
        assert repository.add_fact(fact) is True
        assert repository.add_fact(fact) is False

    def test_alert_ordering(self, repository: InMemoryRepository) -> None:
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

    # -- helpers -----------------------------------------------------------------

    @staticmethod
    def _order(status: OrderStatus = OrderStatus.ACKNOWLEDGED) -> OrderSnapshot:
        return OrderSnapshot(
            intent_id="bt-oi_1",
            decision_id="bt-dec_1",
            instrument="SPY",
            side=OrderSide.BUY,
            quantity=8,
            filled_quantity=0,
            remaining_quantity=8,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            client_order_id="TQ-bt-1",
            status=status,
        )

    @staticmethod
    def _fact(minutes: int = 0) -> Fact:
        occurred = AT + dt.timedelta(minutes=minutes)
        return Fact(
            fact_id=f"fact:test{minutes:04d}",
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


class TestWorkerLease:
    """Acceptance gate 8: two instances competing for the lease, only one may submit."""

    def _lease(self, *, seconds: int = 120) -> tuple[InMemoryLease, list[dt.datetime]]:
        now = [AT]
        lease = InMemoryLease("paper-exec", clock=lambda: now[0], lease_seconds=seconds)
        return lease, now

    def test_first_owner_wins(self) -> None:
        lease, _ = self._lease()
        acquired = lease.acquire("worker-a")
        assert acquired is not None
        assert acquired.owner == "worker-a"
        assert lease.state().is_held

    def test_second_owner_is_refused_while_the_lease_is_live(self) -> None:
        lease, _ = self._lease()
        lease.acquire("worker-a")
        assert lease.acquire("worker-b") is None
        assert lease.state().owner == "worker-a"

    def test_expired_lease_can_be_taken_over_and_reports_the_previous_owner(self) -> None:
        lease, now = self._lease(seconds=60)
        lease.acquire("worker-a")
        now[0] = AT + dt.timedelta(seconds=61)
        taken = lease.acquire("worker-b")
        assert taken is not None
        assert taken.previous_owner == "worker-a"
        assert "took over" in taken.describe()

    def test_reacquiring_own_lease_is_not_a_takeover(self) -> None:
        lease, _ = self._lease()
        lease.acquire("worker-a")
        again = lease.acquire("worker-a")
        assert again is not None
        assert again.previous_owner is None

    def test_heartbeat_extends_and_is_owner_only(self) -> None:
        lease, now = self._lease(seconds=60)
        lease.acquire("worker-a")
        now[0] = AT + dt.timedelta(seconds=30)
        assert lease.heartbeat("worker-a") is True
        assert lease.heartbeat("worker-b") is False
        now[0] = AT + dt.timedelta(seconds=80)
        # 80s after the original grant, but only 50s after the heartbeat.
        assert lease.state().holder_is_live(now[0])

    def test_heartbeat_after_expiry_fails(self) -> None:
        lease, now = self._lease(seconds=60)
        lease.acquire("worker-a")
        now[0] = AT + dt.timedelta(seconds=61)
        assert lease.heartbeat("worker-a") is False

    def test_release_is_owner_only(self) -> None:
        lease, _ = self._lease()
        lease.acquire("worker-a")
        assert lease.release("worker-b") is False
        assert lease.state().owner == "worker-a"
        assert lease.release("worker-a") is True
        assert not lease.state().is_held

    def test_anonymous_owner_is_refused(self) -> None:
        lease, _ = self._lease()
        with pytest.raises(LeaseError, match="non-empty"):
            lease.acquire("")

    def test_non_positive_lease_seconds_is_refused(self) -> None:
        with pytest.raises(LeaseError, match="positive"):
            InMemoryLease("x", clock=lambda: AT, lease_seconds=0)
