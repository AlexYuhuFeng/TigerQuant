"""ReconnectingRepository: a backoff retry must get a FRESH connection.

The gap this closes was real and would have been invisible in production until the exact moment it
mattered. The supervisor's backoff tier retried the CYCLE, but the monitor held the original
connection, so against a database that had actually restarted -- the case backoff exists for --
every retry failed identically and the loop halted having never once tried a fresh connection.

The tests below are mostly about the two decisions that shape the design:

* it reconnects on the NEXT call and propagates the failure UNCHANGED, so the supervisor's
  classifier still sees the real exception rather than a substitute;
* and it delegates by attribute, because the concrete repository needs the optional `db` extra and
  this module must import without it.
"""

from __future__ import annotations

import pytest

from tigerquant.db.reconnect import ReconnectingRepository


class DbDown(ConnectionError):
    """Stands in for a driver's connection error."""


def is_dead(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionError)


class FakeConnection:
    """A connection that can be made to fail, and records whether it was closed."""

    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if not self.alive:
            msg = "already gone"
            raise DbDown(msg)


class FakeRepository:
    __slots__ = ("calls", "connection")

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls: list[str] = []

    def query(self) -> str:
        self.calls.append("query")
        if not self.connection.alive:
            msg = "connection is gone"
            raise DbDown(msg)
        return "rows"

    def ping(self) -> str:
        return "pong"

    @property
    def mode(self) -> str:
        return "BACKTEST"


class Harness:
    """A connect/repository pair whose connections can be killed between calls."""

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.repositories: list[FakeRepository] = []

    def connect(self) -> FakeConnection:
        connection = FakeConnection()
        self.connections.append(connection)
        return connection

    def repository_for(self, connection: FakeConnection) -> FakeRepository:
        repository = FakeRepository(connection)
        self.repositories.append(repository)
        return repository

    def build(self) -> ReconnectingRepository:
        return ReconnectingRepository(self.connect, self.repository_for, is_dead=is_dead)


class TestItConnectsLazily:
    def test_nothing_is_connected_until_first_use(self) -> None:
        """A supervisor that connects at construction would fail before it could report why."""
        harness = Harness()
        proxy = harness.build()
        assert proxy.connected is False
        assert harness.connections == []

    def test_first_use_connects(self) -> None:
        harness = Harness()
        proxy = harness.build()
        assert proxy.query() == "rows"
        assert proxy.connected is True
        assert len(harness.connections) == 1

    def test_the_property_does_not_count_as_a_use(self) -> None:
        harness = Harness()
        assert harness.build().connected is False
        assert harness.connections == []


class TestItReconnectsAfterADeath:
    def test_a_connection_failure_is_re_raised_unchanged(self) -> None:
        """The supervisor's classifier must see the REAL exception. A proxy that replaced it
        would have backoff deciding on a substitute error."""
        harness = Harness()
        proxy = harness.build()
        proxy.query()  # the proxy is LAZY: no connection exists until first use
        harness.connections[0].alive = False
        with pytest.raises(DbDown, match="connection is gone"):
            proxy.query()

    def test_a_failure_marks_it_disconnected_for_the_next_call(self) -> None:
        harness = Harness()
        proxy = harness.build()
        proxy.query()  # the proxy is LAZY: no connection exists until first use
        harness.connections[0].alive = False
        with pytest.raises(DbDown):
            proxy.query()
        assert proxy.connected is False

    def test_the_old_connection_is_closed(self) -> None:
        harness = Harness()
        proxy = harness.build()
        proxy.query()  # the proxy is LAZY: no connection exists until first use
        harness.connections[0].alive = False
        with pytest.raises(DbDown):
            proxy.query()
        assert harness.connections[0].closed is True

    def test_it_does_not_reconnect_during_the_failing_call(self) -> None:
        """Reconnecting inside the call would hide the failure from the backoff accounting and
        could block for an unbounded time inside a single cycle."""
        harness = Harness()
        proxy = harness.build()
        proxy.query()  # the proxy is LAZY: no connection exists until first use
        harness.connections[0].alive = False
        with pytest.raises(DbDown):
            proxy.query()
        assert len(harness.connections) == 1, (
            "one connection, not two -- the retry is the next call"
        )

    def test_the_next_call_reconnects(self) -> None:
        """THE FIX. Before this, the next call used the same dead connection and failed
        identically, so a restarted database could never be reached."""
        harness = Harness()
        proxy = harness.build()
        proxy.query()  # the proxy is LAZY: no connection exists until first use
        harness.connections[0].alive = False
        with pytest.raises(DbDown):
            proxy.query()

        assert proxy.query() == "rows", "the retry must reach a FRESH connection"
        assert len(harness.connections) == 2
        assert proxy.connected is True

    def test_the_reconnect_count_is_reported(self) -> None:
        """ "It recovered" and "it never actually dropped" are different claims."""
        harness = Harness()
        proxy = harness.build()
        proxy.query()
        assert proxy.reconnects == 0

        harness.connections[0].alive = False
        with pytest.raises(DbDown):
            proxy.query()
        proxy.query()
        assert proxy.reconnects == 1

    def test_several_outages_each_reconnect(self) -> None:
        harness = Harness()
        proxy = harness.build()
        proxy.query()
        for index in range(3):
            harness.connections[index].alive = False
            with pytest.raises(DbDown):
                proxy.query()
            assert proxy.query() == "rows"
        assert proxy.reconnects == 3


class TestANonConnectionFailureDoesNotDropTheConnection:
    def test_it_is_propagated_and_the_connection_is_kept(self) -> None:
        """A query that returns the wrong thing is not a reason to reconnect. Dropping the
        connection on every error would turn one bad query into a reconnect storm."""
        harness = Harness()

        class Boom(Exception):
            pass

        class BrokenQueryRepository(FakeRepository):
            """A subclass rather than a monkeypatched instance: FakeRepository declares
            __slots__, so an attribute cannot be attached after construction."""

            def failing(self) -> str:
                msg = "a logic error"
                raise Boom(msg)

        def repository_for(connection: FakeConnection) -> FakeRepository:
            return BrokenQueryRepository(connection)

        proxy = ReconnectingRepository(harness.connect, repository_for, is_dead=is_dead)
        proxy.ping()  # connect first, so there is a connection to KEEP
        with pytest.raises(Boom):
            proxy.failing()  # type: ignore[attr-defined]
        assert proxy.connected is True, "a non-connection error must not drop the connection"

    def test_the_classifier_is_consulted_not_assumed(self) -> None:
        """`is_dead` is injected, so the proxy does not hardcode what a dead connection looks
        like -- the driver's hierarchy is the caller's business."""
        harness = Harness()
        never = ReconnectingRepository(
            harness.connect, harness.repository_for, is_dead=lambda _e: False
        )
        never.query()
        harness.connections[0].alive = False
        with pytest.raises(DbDown):
            never.query()
        assert never.connected is True


class TestDelegation:
    def test_plain_attributes_pass_through(self) -> None:
        harness = Harness()
        assert harness.build().mode == "BACKTEST"

    def test_non_callable_attributes_are_not_wrapped(self) -> None:
        harness = Harness()
        assert harness.build().mode == "BACKTEST"

    def test_methods_reach_the_underlying_repository(self) -> None:
        harness = Harness()
        proxy = harness.build()
        assert proxy.ping() == "pong"
        assert harness.repositories[0].calls == []

    def test_the_same_repository_is_reused_while_connected(self) -> None:
        """One connection per outage, not one per call."""
        harness = Harness()
        proxy = harness.build()
        for _ in range(5):
            proxy.query()
        assert len(harness.repositories) == 1

    def test_it_imports_no_driver(self) -> None:
        """The concrete repository needs the optional `db` extra; this module must import
        without it, which is why it delegates rather than subclasses."""
        import inspect

        from tigerquant.db import reconnect

        source = inspect.getsource(reconnect)
        for forbidden in ("import psycopg", "from psycopg"):
            assert forbidden not in source


class TestExplicitDisconnect:
    def test_a_caller_can_drop_the_connection(self) -> None:
        """For a caller that knows the database went away by some route other than an exception."""
        harness = Harness()
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        assert proxy.connected is False
        assert harness.connections[0].closed is True

    def test_a_broken_connection_that_cannot_be_closed_is_still_dropped(self) -> None:
        """Closing an already-dead connection is expected to fail, and raising would replace the
        real error with a bookkeeping one."""
        harness = Harness()
        proxy = harness.build()
        proxy.query()
        harness.connections[0].alive = False
        proxy.disconnect()  # must not raise
        assert proxy.connected is False

    def test_disconnecting_twice_is_harmless(self) -> None:
        harness = Harness()
        proxy = harness.build()
        proxy.query()
        proxy.disconnect()
        proxy.disconnect()
        assert proxy.connected is False
