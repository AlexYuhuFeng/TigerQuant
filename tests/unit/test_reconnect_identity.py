"""Identity is re-verified on EVERY connection, not only the first.

The guard added two rounds ago runs once, when the process starts. **A reconnect is exactly the
moment the guarantee it established can silently stop holding** -- a failover to a replica, a DNS
change, a load balancer moving the connection to another node. Any of them can land the process on
a DIFFERENT database, and the run would carry on reporting against it as if nothing had happened.

That is the same false-negative shape as the original defect, one layer down: not "we connected to
the wrong database" but **"we connected to the right one, and then quietly moved"**.
"""

from __future__ import annotations

import pytest

from tigerquant.db.reconnect import ReconnectingRepository


class DbDown(ConnectionError):
    pass


class Connection:
    """A connection that knows which database it landed on."""

    def __init__(self, database: str, *, alive: bool = True) -> None:
        self.database = database
        self.alive = alive
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Repository:
    __slots__ = ("connection",)

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def query(self) -> str:
        if not self.connection.alive:
            msg = "connection is gone"
            raise DbDown(msg)
        return self.connection.database


class Harness:
    """Connects to a sequence of databases, so a reconnect can land somewhere else."""

    def __init__(self, databases: list[str]) -> None:
        self._databases = list(databases)
        self.connections: list[Connection] = []
        self.verified: list[str] = []

    def connect(self) -> Connection:
        database = self._databases.pop(0) if len(self._databases) > 1 else self._databases[0]
        connection = Connection(database)
        self.connections.append(connection)
        return connection

    @staticmethod
    def repository_for(connection: Connection) -> Repository:
        return Repository(connection)

    def build(self, *, expected: str = "BACKTEST") -> ReconnectingRepository:
        def check(connection: Connection) -> None:
            self.verified.append(connection.database)
            if connection.database != expected:
                msg = f"database is {connection.database}, expected {expected}"
                raise RuntimeError(msg)

        return ReconnectingRepository(
            self.connect,
            self.repository_for,
            is_dead=lambda exc: isinstance(exc, DbDown),
            verify_on_connect=check,
        )


class TestItVerifiesEveryConnection:
    def test_the_first_connection_is_verified(self) -> None:
        harness = Harness(["BACKTEST"])
        harness.build().query()
        assert harness.verified == ["BACKTEST"]

    def test_the_reconnection_is_verified_too(self) -> None:
        """The whole point. A guard that runs once expires at the first reconnect."""
        harness = Harness(["BACKTEST", "BACKTEST"])
        proxy = harness.build()
        proxy.query()
        harness.connections[0].alive = False
        with pytest.raises(DbDown):
            proxy.query()
        proxy.query()
        assert harness.verified == ["BACKTEST", "BACKTEST"]
        assert len(harness.verified) == 2

    def test_verification_does_not_run_when_the_connection_is_reused(self) -> None:
        """Once per CONNECTION, not once per call. Verifying on every query would be a round trip
        per query for a guarantee that cannot change while the connection is up."""
        harness = Harness(["BACKTEST"])
        proxy = harness.build()
        for _ in range(5):
            proxy.query()
        assert harness.verified == ["BACKTEST"]


class TestAReconnectToTheWrongDatabaseIsRefused:
    def test_it_raises(self) -> None:
        """The scenario: the connection drops, and the reconnect lands elsewhere."""
        harness = Harness(["BACKTEST", "PAPER"])
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        with pytest.raises(RuntimeError, match="expected BACKTEST"):
            proxy.query()

    def test_no_connection_is_kept_after_a_failed_verification(self) -> None:
        """A refused reconnect must not leave a working repository pointed at the wrong
        database."""
        harness = Harness(["BACKTEST", "PAPER"])
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        with pytest.raises(RuntimeError):
            proxy.query()
        assert proxy.connected is False

    def test_the_rejected_connection_is_closed(self) -> None:
        harness = Harness(["BACKTEST", "PAPER"])
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        with pytest.raises(RuntimeError):
            proxy.query()
        assert harness.connections[-1].closed is True

    def test_the_reconnect_count_is_not_inflated_by_a_refusal(self) -> None:
        """ "It reconnected 3 times" must mean three SUCCESSFUL reconnections, or the number
        lies about how healthy the loop was."""
        harness = Harness(["BACKTEST", "PAPER"])
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        with pytest.raises(RuntimeError):
            proxy.query()
        assert proxy.reconnects == 0

    def test_the_verification_that_failed_is_recorded(self) -> None:
        harness = Harness(["BACKTEST", "PAPER"])
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        with pytest.raises(RuntimeError):
            proxy.query()
        assert harness.verified == ["BACKTEST", "PAPER"]

    def test_a_wrong_first_connection_is_refused_at_startup(self) -> None:
        """Not only reconnects: the first connection is verified by the same path, so there is one
        rule rather than two that could disagree."""
        harness = Harness(["PAPER", "BACKTEST"])
        with pytest.raises(RuntimeError, match="expected BACKTEST"):
            harness.build().query()

    def test_it_recovers_if_the_database_comes_back_correct(self) -> None:
        """A refusal must not poison the proxy for ever."""
        harness = Harness(["BACKTEST", "PAPER", "BACKTEST"])
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        with pytest.raises(RuntimeError):
            proxy.query()
        # Bypass the scripted sequence: the next connect returns the last entry, BACKTEST.
        assert proxy.query() == "BACKTEST"


class TestWithoutVerificationNothingChanges:
    def test_a_proxy_with_no_verifier_still_reconnects(self) -> None:
        """The verifier is optional, so nothing that did not ask for it is changed."""
        harness = Harness(["BACKTEST", "PAPER"])
        proxy = ReconnectingRepository(
            harness.connect,
            harness.repository_for,
            is_dead=lambda exc: isinstance(exc, DbDown),
        )
        proxy.query()
        proxy.disconnect()
        assert proxy.query() == "PAPER", "unverified, so it uses whatever it connects to"
        assert harness.verified == []

    def test_the_verifier_is_optional_in_the_signature(self) -> None:
        import inspect

        parameters = inspect.signature(ReconnectingRepository.__init__).parameters
        assert parameters["verify_on_connect"].default is None
